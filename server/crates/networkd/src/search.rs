//! `/v1/search`: one box over registrations and hexes, flight numbers,
//! routes (two places), fleets (an operator and a type), airports and
//! airlines, as one ranked list. Port of the Python service's
//! `routes_search.py`, answered from the nightly snapshot and cached per
//! query until the next one.
//!
//! What depends on Postgres's rules is computed by Postgres at export
//! time: upper() of names (its en_US ctype, not Python's str.upper), an
//! airport's traffic, and the text orders (rank tables). LIKE keeps
//! Postgres semantics: case-sensitive, `%` and `_` typed by a user are
//! wildcards, backslash escapes. Where Python's order among ties comes
//! from Postgres's sort (a route's flights, the airport a place name
//! resolves to, near misses), pgsort reproduces it.

use std::collections::HashSet;
use std::sync::Arc;

use axum::extract::{Request, State};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use rusqlite::{OptionalExtension, ToSql};

use crate::http::{client_ip, json, peer_of, query_param, throttle, ApiError};
use crate::pyjson::{round_to, write_float, write_str, Obj};
use crate::refdb::Conn;
use crate::state::App;

const CACHE: &str = "public, s-maxage=43200";
const PER_KIND: usize = 5;
const EXACT: i64 = 100;
const PREFIX: i64 = 60;
const WORD: i64 = 40;
const NEAR: i64 = 20;

const NEEDS: &[&str] = &[
    "ref_airframes", "ref_airlines", "ref_airports", "ref_schedule", "ref_types", "search_airports",
    "search_airlines", "search_types", "rank_callsign", "rank_ref_airframes_registration",
    "rank_ref_airports_name", "rank_ref_airlines_name",
];

/// A score the way Python holds it: an int stays an int (and prints as
/// one), anything lifted by traffic is a float.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Score {
    Int(i64),
    Float(f64),
}

impl Score {
    fn value(self) -> f64 {
        match self {
            Score::Int(i) => i as f64,
            Score::Float(f) => f,
        }
    }
    fn plus(self, x: f64) -> Score {
        Score::Float(self.value() + x)
    }
    fn plus_int(self, i: i64) -> Score {
        match self {
            Score::Int(a) => Score::Int(a + i),
            Score::Float(f) => Score::Float(f + i as f64),
        }
    }
    fn rounded(self) -> Score {
        match self {
            Score::Int(i) => Score::Int(i),
            Score::Float(f) => Score::Float(round_to(f, 1)),
        }
    }
}

struct Hit {
    kind: &'static str,
    id: String,
    label: String,
    detail: Option<String>,
    score: Score,
}

impl Hit {
    fn kind_order(&self) -> u8 {
        match self.kind {
            "flight" => 0,
            "aircraft" => 1,
            "airport" => 2,
            _ => 3,
        }
    }
}

/// Traffic as a tie-breaker inside a match class, never across.
fn lift(n: i64) -> f64 {
    (1.0 + n.max(0) as f64).log10() * 4.0
}

/// Python's `" ".join(q.strip().upper().split())`.
fn norm(q: &str) -> String {
    q.trim().to_uppercase().split_whitespace().collect::<Vec<_>>().join(" ")
}

fn is_digit(c: char) -> bool {
    c.is_numeric()
}

fn is_alpha_word(s: &str) -> bool {
    !s.is_empty() && s.chars().all(char::is_alphabetic)
}

struct Shape {
    aircraft: bool,
    flight: bool,
    airport: bool,
    airline: bool,
    pair: bool,
}

fn shape(q: &str) -> Shape {
    let compact: String = q.chars().filter(|c| *c != ' ' && *c != '-').collect();
    let n = compact.chars().count();
    let has_digit = compact.chars().any(is_digit);
    let alpha = is_alpha_word(&compact);
    let tokens = q.split(' ').count();
    let first_two: String = compact.chars().take(2).collect();
    Shape {
        aircraft: has_digit || q.contains('-') || n <= 6,
        flight: n >= 3 && is_alpha_word(&first_two) && first_two.chars().count() == 2 && (has_digit || n <= 4) && tokens == 1,
        airport: alpha && (2..=40).contains(&n),
        airline: alpha && (2..=40).contains(&n),
        pair: tokens == 2,
    }
}

// ---- aircraft ---------------------------------------------------------

#[derive(Clone)]
struct Frame {
    hex: String,
    registration: Option<String>,
    type_code: Option<String>,
    operator_name: Option<String>,
    type_name: Option<String>,
    airline: Option<String>,
}

fn frame_hit(f: &Frame, score: i64) -> Hit {
    let bits = [f.type_name.clone().or(f.type_code.clone()), f.airline.clone().or(f.operator_name.clone())];
    let detail = bits.iter().flatten().filter(|b| !b.is_empty()).cloned().collect::<Vec<_>>().join(" · ");
    Hit {
        kind: "aircraft",
        id: f.hex.clone(),
        label: f.registration.clone().filter(|r| !r.is_empty()).unwrap_or_else(|| f.hex.to_uppercase()),
        detail: (!detail.is_empty()).then_some(detail),
        score: Score::Int(score),
    }
}

/// `_frames`: airframes matching `cond`, first `limit` by registration
/// (Postgres's order, nulls last), with their type and observed airline.
/// `scan` is the order Postgres's plan reads candidates in; its bounded
/// sort (pgsort::top_n) then decides among equal registrations.
fn frames(c: &Conn, cond: &str, scan: &str, params: &[&dyn ToSql], limit: usize) -> rusqlite::Result<Vec<Frame>> {
    frames_upto(c, cond, scan, params, limit, None)
}

/// `frames` for a pattern with no literal prefix (`%…`, `_…`), which no
/// index narrows: walk registrations in rank order until the limit is
/// met, then decide among only the rows that can tie with the last one.
fn frames_wild(c: &Conn, cond: &str, params: &[&dyn ToSql], limit: usize) -> rusqlite::Result<Vec<Frame>> {
    // such a walk can cost most of a second: one at a time, so whatever
    // arrives, wildcard searches hold at most one core
    static ONE_AT_A_TIME: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _turn = ONE_AT_A_TIME.lock().unwrap_or_else(|e| e.into_inner());
    let last: Option<i64> = c
        .prepare_cached(&format!(
            "SELECT r.rank FROM rank_ref_airframes_registration r JOIN ref_airframes f ON f.hex = r.key \
             WHERE {cond} ORDER BY r.rank LIMIT 1 OFFSET {}",
            limit.saturating_sub(1)
        ))?
        .query_row(params, |r| r.get(0))
        .optional()?;
    frames_upto(c, cond, "f.rowid", params, limit, last)
}

fn frames_upto(
    c: &Conn,
    cond: &str,
    scan: &str,
    params: &[&dyn ToSql],
    limit: usize,
    max_rank: Option<i64>,
) -> rusqlite::Result<Vec<Frame>> {
    let cond = match max_rank {
        Some(m) => format!("({cond}) AND r.rank <= {m}"),
        None => cond.to_string(),
    };
    let sql = format!(
        "SELECT f.hex, f.registration, f.type_code, f.operator_name, t.name, al.name, r.rank \
         FROM ref_airframes f JOIN rank_ref_airframes_registration r ON r.key = f.hex \
         LEFT JOIN ref_types t ON t.designator = f.type_code \
         LEFT JOIN ref_airlines al ON al.icao = f.operator_icao \
         WHERE {cond} ORDER BY {scan}"
    );
    let rows: Vec<(Frame, i64)> = c
        .prepare_cached(&sql)?
        .query_map(params, |r| {
            Ok((
                Frame {
                    hex: r.get(0)?,
                    registration: r.get(1)?,
                    type_code: r.get(2)?,
                    operator_name: r.get(3)?,
                    type_name: r.get(4)?,
                    airline: r.get(5)?,
                },
                r.get(6)?,
            ))
        })?
        .collect::<rusqlite::Result<_>>()?;
    let top = crate::pgsort::top_n(rows, limit, &|x: &(Frame, i64), y: &(Frame, i64)| x.1.cmp(&y.1));
    Ok(top.into_iter().map(|(f, _)| f).collect())
}

fn aircraft(c: &Conn, q: &str) -> rusqlite::Result<Vec<Hit>> {
    // Postgres reads these through its prefix indexes (byte order, then
    // storage order); the hex prefix is a table scan
    let wild = q.starts_with(['%', '_']);
    let pick = |cond: &str, scan: &str, pattern: &String| {
        if wild {
            frames_wild(c, cond, &[pattern], PER_KIND)
        } else {
            frames(c, cond, scan, &[pattern], PER_KIND)
        }
    };
    let mut rows: Vec<(Frame, i64)> = pick("f.registration LIKE ?1 ESCAPE '\\'", "f.registration, f.rowid", &format!("{q}%"))?
        .into_iter()
        .map(|f| {
            let s = if f.registration.as_deref() == Some(q) { EXACT } else { PREFIX };
            (f, s)
        })
        .collect();
    let bare: String = q.replace('-', "");
    if !q.contains('-') && bare.chars().any(is_digit) && rows.len() < PER_KIND {
        rows.extend(
            pick("replace(f.registration, '-', '') LIKE ?1 ESCAPE '\\'", "replace(f.registration, '-', ''), f.rowid", &format!("{bare}%"))?
                .into_iter()
                .map(|f| (f, PREFIX)),
        );
    }
    if q.chars().count() >= 3 && q.chars().all(|c| "0123456789ABCDEF".contains(c)) && rows.len() < PER_KIND {
        let lower = q.to_lowercase();
        rows.extend(frames(c, "f.hex LIKE ?1 ESCAPE '\\'", "f.rowid", &[&format!("{lower}%")], PER_KIND)?.into_iter().map(|f| {
            let s = if f.hex == lower { EXACT } else { PREFIX };
            (f, s)
        }));
    }
    let mut seen = HashSet::new();
    let mut out = vec![];
    for (f, s) in rows {
        if seen.insert(f.hex.clone()) {
            out.push(frame_hit(&f, s));
        }
    }
    out.truncate(PER_KIND);
    Ok(out)
}

fn fleet(c: &Conn, q: &str) -> rusqlite::Result<Vec<Hit>> {
    let (a, b) = q.split_once(' ').unwrap();
    for (airline_q, type_q) in [(a, b), (b, a)] {
        let airline: Option<(String, String)> = c
            .prepare_cached(
                "SELECT al.icao, al.name FROM ref_airlines al JOIN search_airlines s ON s.icao = al.icao \
                 JOIN rank_ref_airlines_name r ON r.key = al.icao \
                 WHERE al.icao = ?1 OR al.iata = ?1 OR s.name_upper LIKE ?2 ESCAPE '\\' ORDER BY r.rank LIMIT 1",
            )?
            .query_row((airline_q, format!("{airline_q}%")), |r| Ok((r.get(0)?, r.get(1)?)))
            .optional()?;
        let Some((icao, name)) = airline else { continue };
        let types: Vec<String> = c
            .prepare_cached(
                "SELECT t.designator FROM ref_types t JOIN search_types s ON s.designator = t.designator \
                 WHERE t.designator = ?1 OR s.name_upper LIKE ?2 ESCAPE '\\' ORDER BY t.rowid LIMIT 20",
            )?
            .query_map((type_q, format!("%{type_q}%")), |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        if types.is_empty() {
            continue;
        }
        // observed operator first; the registry's operator name for
        // airframes the network has not watched fly yet (Python upper)
        let marks = (0..types.len()).map(|i| format!("?{}", i + 3)).collect::<Vec<_>>().join(",");
        let norm_like = format!("{}%", name.to_uppercase());
        let mut params: Vec<&dyn ToSql> = vec![&icao, &norm_like];
        params.extend(types.iter().map(|t| t as &dyn ToSql));
        let rows = frames(
            c,
            &format!("(f.operator_icao = ?1 OR f.operator_norm LIKE ?2 ESCAPE '\\') AND f.type_code IN ({marks})"),
            "f.rowid",
            &params,
            PER_KIND,
        )?;
        return Ok(rows.iter().map(|f| frame_hit(f, WORD)).collect());
    }
    Ok(vec![])
}

// ---- flights and routes ------------------------------------------------

fn flight_hit(callsign: &str, n: i64, org: &str, dst: &str, legs: i64, base: i64) -> Hit {
    let route = if legs == 1 { format!("{org} \u{2192} {dst}") } else { format!("{legs} legs") };
    Hit {
        kind: "flight",
        id: callsign.to_string(),
        label: callsign.to_string(),
        detail: Some(format!("{route} · {n} flights")),
        score: Score::Int(base).plus(lift(n)),
    }
}

fn flights(c: &Conn, app: &App, q: &str) -> rusqlite::Result<Vec<Hit>> {
    let prefix: String = q.replace(' ', "");
    let mut prefixes = vec![prefix.clone()];
    // an IATA flight number (SQ322) is also its ICAO callsign (SIA322)
    if prefix.chars().nth(2).is_some_and(is_digit) {
        let two: String = prefix.chars().take(2).collect();
        let icao: Option<String> = c
            .prepare_cached("SELECT icao FROM ref_airlines WHERE iata = ?1 ORDER BY rowid LIMIT 1")?
            .query_row([&two], |r| r.get(0))
            .optional()?;
        if let Some(icao) = icao {
            prefixes.push(format!("{icao}{}", prefix.chars().skip(2).collect::<String>()));
        }
    }
    let mut out = vec![];
    let mut seen = HashSet::new();
    for p in &prefixes {
        let mut stmt = c.prepare_cached(
            "SELECT s.callsign, SUM(s.n_flights), MIN(s.org), MIN(s.dst), COUNT(*) FROM ref_schedule s \
             JOIN rank_callsign r ON r.key = s.callsign WHERE s.callsign LIKE ?1 ESCAPE '\\' \
             GROUP BY s.callsign ORDER BY 2 DESC, MIN(r.rank) LIMIT 5",
        )?;
        let rows: Vec<(String, i64, String, String, i64)> = stmt
            .query_map([format!("{p}%")], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?
            .collect::<rusqlite::Result<_>>()?;
        for (cs, n, org, dst, legs) in rows {
            // a bare prefix flown as a callsign ("CDG") is noise
            if seen.contains(&cs) || !cs.chars().any(is_digit) {
                continue;
            }
            seen.insert(cs.clone());
            let base = if &cs == p { EXACT } else { PREFIX };
            out.push(flight_hit(&cs, n, &org, &dst, legs, base));
        }
    }
    if out.len() < PER_KIND && prefix.chars().any(is_digit) && app.legs.available() {
        for p in &prefixes {
            let rows = app.legs.with_conn(|lc| crate::legs::callsigns(lc, p, PER_KIND)).unwrap_or_default();
            for (cs, n, last) in rows {
                if !seen.insert(cs.clone()) {
                    continue;
                }
                let base = if &cs == p { EXACT } else { PREFIX };
                out.push(Hit {
                    kind: "flight",
                    label: cs.clone(),
                    detail: last.map(|l| format!("{n} flights, last {l}")),
                    id: cs,
                    score: Score::Int(base).plus(lift(n) - 1.0),
                });
            }
        }
    }
    out.truncate(PER_KIND);
    Ok(out)
}

/// A code, or a city or airport name, to one airport code; cities with
/// several airports resolve to the busiest.
fn airport_code(rows: &[AirportRow], word: &str) -> Option<String> {
    // ORDER BY exact first, traffic DESC LIMIT 1, over the scan's order
    let starts = format!("{word}%");
    let found: Vec<(usize, i64, i64)> = rows
        .iter()
        .enumerate()
        .filter_map(|(k, a)| {
            let exact = a.iata.as_deref() == Some(word) || a.ident == word;
            (exact || opt_like(&a.muni_upper, &starts) || opt_like(&a.name_upper, &starts))
                .then_some((k, if exact { 0 } else { 1 }, a.traffic))
        })
        .collect();
    let top = crate::pgsort::top_n(found, 1, &|x: &(usize, i64, i64), y: &(usize, i64, i64)| x.1.cmp(&y.1).then(y.2.cmp(&x.2)));
    top.first().map(|t| {
        let a = &rows[t.0];
        a.iata.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| a.ident.clone())
    })
}

fn route(c: &Conn, rows: &[AirportRow], q: &str) -> rusqlite::Result<Vec<Hit>> {
    let (a, b) = q.split_once(' ').unwrap();
    let (Some(org), Some(dst)) = (airport_code(rows, a), airport_code(rows, b)) else { return Ok(vec![]) };
    if org == dst {
        return Ok(vec![]);
    }
    let rows: Vec<(String, i64)> = c
        .prepare_cached("SELECT callsign, n_flights FROM ref_schedule WHERE org = ?1 AND dst = ?2 ORDER BY rowid")?
        .query_map((&org, &dst), |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    let rows = crate::pgsort::top_n(rows, PER_KIND, &|x: &(String, i64), y: &(String, i64)| y.1.cmp(&x.1));
    Ok(rows.iter().map(|(cs, n)| flight_hit(cs, *n, &org, &dst, 1, WORD)).collect())
}

// ---- airports and airlines ---------------------------------------------

/// Postgres's LIKE on characters: `%` any run, `_` any one character,
/// backslash takes the next character literally; case-sensitive.
fn like(s: &str, pattern: &str) -> bool {
    #[derive(Clone, Copy, PartialEq)]
    enum P {
        Any,
        One,
        Lit(char),
    }
    let mut pat = vec![];
    let mut it = pattern.chars();
    while let Some(ch) = it.next() {
        pat.push(match ch {
            '%' => P::Any,
            '_' => P::One,
            '\\' => P::Lit(it.next().unwrap_or('\\')),
            c => P::Lit(c),
        });
    }
    let s: Vec<char> = s.chars().collect();
    // iterative wildcard match with backtracking to the last %
    let (mut i, mut j) = (0usize, 0usize);
    let (mut star, mut mark) = (None::<usize>, 0usize);
    while i < s.len() {
        if j < pat.len() && (pat[j] == P::One || pat[j] == P::Lit(s[i])) {
            i += 1;
            j += 1;
        } else if j < pat.len() && pat[j] == P::Any {
            star = Some(j);
            mark = i;
            j += 1;
        } else if let Some(st) = star {
            j = st + 1;
            mark += 1;
            i = mark;
        } else {
            return false;
        }
    }
    while j < pat.len() && pat[j] == P::Any {
        j += 1;
    }
    j == pat.len()
}

/// The large and medium airports, in storage order, with what the
/// searches read: built once per snapshot, scanned in memory.
struct AirportRow {
    ident: String,
    iata: Option<String>,
    name: Option<String>,
    municipality: Option<String>,
    iso_country: Option<String>,
    name_upper: Option<String>,
    muni_upper: Option<String>,
    traffic: i64,
    /// Postgres's order of names (nulls last)
    name_rank: i64,
}

fn airport_rows(c: &Conn) -> rusqlite::Result<Vec<AirportRow>> {
    c.prepare(
        "SELECT a.ident, a.iata, a.name, a.municipality, a.iso_country, s.name_upper, s.municipality_upper, s.traffic, \
         coalesce(r.rank, 9223372036854775807) FROM ref_airports a JOIN search_airports s ON s.ident = a.ident \
         LEFT JOIN rank_ref_airports_name r ON r.key = a.ident \
         WHERE a.kind IN ('large_airport', 'medium_airport') ORDER BY a.rowid",
    )?
    .query_map([], |r| {
        Ok(AirportRow {
            ident: r.get(0)?,
            iata: r.get(1)?,
            name: r.get(2)?,
            municipality: r.get(3)?,
            iso_country: r.get(4)?,
            name_upper: r.get(5)?,
            muni_upper: r.get(6)?,
            traffic: r.get(7)?,
            name_rank: r.get(8)?,
        })
    })?
    .collect()
}

fn opt_like(s: &Option<String>, pattern: &str) -> bool {
    s.as_deref().is_some_and(|s| like(s, pattern))
}

/// fuzzystrmatch's levenshtein, over characters.
fn levenshtein(a: &str, b: &str) -> usize {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    let mut cur = vec![0; b.len() + 1];
    for i in 1..=a.len() {
        cur[0] = i;
        for j in 1..=b.len() {
            let sub = prev[j - 1] + usize::from(a[i - 1] != b[j - 1]);
            cur[j] = sub.min(prev[j] + 1).min(cur[j - 1] + 1);
        }
        std::mem::swap(&mut prev, &mut cur);
    }
    prev[b.len()]
}

/// Postgres's `regexp_split_to_table(text, '\s+')` then `length(w) >= 4
/// AND levenshtein(w, q) <= 2` for some word.
fn near(text: &str, q: &str) -> bool {
    text.split(|c: char| c.is_whitespace()).any(|w| w.chars().count() >= 4 && levenshtein(w, q) <= 2)
}

fn airports(rows: &[AirportRow], q: &str) -> Vec<Hit> {
    let starts = format!("{q}%");
    let word = format!("% {q}%");
    // (row, class)
    let hits: Vec<(usize, i64)> = rows
        .iter()
        .enumerate()
        .filter_map(|(k, a)| {
            let exact = a.iata.as_deref() == Some(q) || a.ident == q;
            let start = opt_like(&a.name_upper, &starts) || opt_like(&a.muni_upper, &starts);
            let cls = if exact {
                EXACT
            } else if start {
                PREFIX
            } else if opt_like(&a.name_upper, &word) {
                WORD
            } else {
                return None;
            };
            Some((k, cls))
        })
        .collect();
    // ORDER BY cls DESC, traffic DESC, iata IS NULL, name LIMIT 5, as
    // Postgres picks among identical names
    let mut hits = crate::pgsort::top_n(hits, PER_KIND, &|x: &(usize, i64), y: &(usize, i64)| {
        let (a, b) = (&rows[x.0], &rows[y.0]);
        y.1.cmp(&x.1)
            .then(b.traffic.cmp(&a.traffic))
            .then(a.iata.is_none().cmp(&b.iata.is_none()))
            .then(a.name_rank.cmp(&b.name_rank))
    });
    if hits.is_empty() && q.chars().count() >= 5 && is_alpha_word(q) {
        // a near miss: Chnagi, Heathro (two edits at most), ORDER BY
        // traffic DESC LIMIT 5 as Postgres picks among ties
        let found: Vec<(usize, i64)> = rows
            .iter()
            .enumerate()
            .filter(|(_, a)| {
                let words = format!("{} {}", a.name_upper.as_deref().unwrap_or(""), a.muni_upper.as_deref().unwrap_or(""));
                near(&words, q)
            })
            .map(|(k, _)| (k, NEAR))
            .collect();
        hits = crate::pgsort::top_n(found, PER_KIND, &|x: &(usize, i64), y: &(usize, i64)| rows[y.0].traffic.cmp(&rows[x.0].traffic));
    }
    hits.into_iter()
        .map(|(k, cls)| {
            let a = &rows[k];
            let iata = a.iata.clone().filter(|s| !s.is_empty());
            let code = iata.clone().unwrap_or_else(|| a.ident.clone());
            let detail = [a.municipality.clone(), a.iso_country.clone()]
                .into_iter()
                .flatten()
                .filter(|b| !b.is_empty())
                .collect::<Vec<_>>()
                .join(" · ");
            let label_name = a.name.clone().filter(|n| !n.is_empty()).unwrap_or_else(|| code.clone());
            Hit {
                kind: "airport",
                label: format!("{label_name} ({code})"),
                id: code,
                detail: (!detail.is_empty()).then_some(detail),
                score: Score::Int(cls).plus(lift(a.traffic)).plus_int(if iata.is_some() { 2 } else { 0 }),
            }
        })
        .collect()
}

fn airlines(c: &Conn, q: &str) -> rusqlite::Result<Vec<Hit>> {
    type Row = (String, Option<String>, String, i64);
    let read = |r: &rusqlite::Row| -> rusqlite::Result<Row> { Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)) };
    // ORDER BY cls DESC, iata IS NULL, name LIMIT 5 over the scan's order
    let found: Vec<(Row, i64)> = c
        .prepare_cached(&format!(
            "SELECT al.icao, al.iata, al.name, CASE WHEN al.icao = ?1 OR al.iata = ?1 THEN {EXACT} \
             WHEN s.name_upper LIKE ?2 ESCAPE '\\' THEN {PREFIX} ELSE {WORD} END AS cls, r.rank \
             FROM ref_airlines al JOIN search_airlines s ON s.icao = al.icao \
             JOIN rank_ref_airlines_name r ON r.key = al.icao \
             WHERE al.icao = ?1 OR al.iata = ?1 OR s.name_upper LIKE ?2 ESCAPE '\\' OR s.name_upper LIKE ?3 ESCAPE '\\' \
             ORDER BY al.rowid"
        ))?
        .query_map((q, format!("{q}%"), format!("% {q}%")), |r| Ok((read(r)?, r.get(4)?)))?
        .collect::<rusqlite::Result<_>>()?;
    let mut rows: Vec<Row> = crate::pgsort::top_n(found, PER_KIND, &|x: &(Row, i64), y: &(Row, i64)| {
        y.0 .3.cmp(&x.0 .3).then(x.0 .1.is_none().cmp(&y.0 .1.is_none())).then(x.1.cmp(&y.1))
    })
    .into_iter()
    .map(|(r, _)| r)
    .collect();
    if rows.is_empty() && q.chars().count() >= 5 && is_alpha_word(q) {
        // ORDER BY name LIMIT 5 among near misses, as Postgres picks
        let mut stmt = c.prepare_cached(&format!(
            "SELECT al.icao, al.iata, al.name, {NEAR}, s.name_upper, r.rank FROM ref_airlines al \
             JOIN search_airlines s ON s.icao = al.icao JOIN rank_ref_airlines_name r ON r.key = al.icao ORDER BY al.rowid"
        ))?;
        let mut all = stmt.query([])?;
        let mut found: Vec<(Row, i64)> = vec![];
        while let Some(r) = all.next()? {
            let name_upper: Option<String> = r.get(4)?;
            if name_upper.as_deref().is_some_and(|n| near(n, q)) {
                found.push((read(r)?, r.get(5)?));
            }
        }
        rows = crate::pgsort::top_n(found, PER_KIND, &|x: &(Row, i64), y: &(Row, i64)| x.1.cmp(&y.1))
            .into_iter()
            .map(|(r, _)| r)
            .collect();
    }
    Ok(rows
        .into_iter()
        .map(|(icao, iata, name, cls)| {
            let iata = iata.filter(|s| !s.is_empty());
            let detail = [Some(icao.clone()), iata.clone()].into_iter().flatten().collect::<Vec<_>>().join(" · ");
            Hit {
                kind: "airline",
                id: icao,
                label: name,
                detail: (!detail.is_empty()).then_some(detail),
                score: Score::Int(cls).plus_int(if iata.is_some() { 2 } else { 0 }),
            }
        })
        .collect())
}

// ---- the route --------------------------------------------------------

fn run(c: &Conn, app: &App, q: &str) -> rusqlite::Result<String> {
    let sh = shape(q);
    let rows = app.refdb.memo("search_airport_rows", c, airport_rows)?;
    let mut results: Vec<Hit> = vec![];
    if sh.pair {
        results.extend(route(c, &rows, q)?);
        results.extend(fleet(c, q)?);
    }
    if sh.aircraft {
        results.extend(aircraft(c, q)?);
    }
    if sh.flight {
        results.extend(flights(c, app, q)?);
    }
    if sh.airport {
        results.extend(airports(&rows, q));
    }
    if sh.airline {
        results.extend(airlines(c, q)?);
    }
    // stable: each kind keeps its own order for equal scores
    results.sort_by(|a, b| b.score.value().total_cmp(&a.score.value()).then(a.kind_order().cmp(&b.kind_order())));
    let mut out = String::with_capacity(1024);
    let mut o = Obj::new(&mut out);
    o.str("q", q);
    let buf = o.key("results");
    buf.push('[');
    for (k, h) in results.iter().enumerate() {
        if k > 0 {
            buf.push(',');
        }
        let mut ho = Obj::new(buf);
        ho.str("kind", h.kind).str("id", &h.id).str("label", &h.label);
        match &h.detail {
            Some(d) => write_str(ho.key("detail"), d),
            None => ho.key("detail").push_str("null"),
        }
        match h.score.rounded() {
            Score::Int(i) => {
                ho.int("score", i);
            }
            Score::Float(f) => write_float(ho.key("score"), f),
        }
        ho.end();
    }
    buf.push(']');
    o.end();
    Ok(out)
}

pub async fn search(State(app): State<Arc<App>>, req: Request) -> Response {
    if !app.refdb.has(NEEDS) {
        return crate::proxy::forward(State(app), req).await;
    }
    // validation before the throttle, as FastAPI's does
    let Some(raw) = query_param(req.uri().query(), "q") else {
        return ApiError::new(422, "invalid_request", "query.q: Field required").into_response();
    };
    let n = raw.chars().count();
    if n < 1 {
        return ApiError::new(422, "invalid_request", "query.q: String should have at least 1 character").into_response();
    }
    if n > 40 {
        return ApiError::new(422, "invalid_request", "query.q: String should have at most 40 characters").into_response();
    }
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "search", app.settings.search_rate_limit) {
        return e.into_response();
    }
    let q = norm(&raw);
    if q.chars().count() < 2 {
        return ApiError::new(422, "invalid_request", "type at least two characters").into_response();
    }
    let stamp = crate::boards::mtime(&app.settings.legs_path).map(|t| format!("{t:?}")).unwrap_or_default();
    let key = format!("search:{stamp}:{q}");
    if let Some(body) = app.refdb.cached(&key) {
        return json(body, CACHE);
    }
    let app2 = app.clone();
    let done = tokio::task::spawn_blocking(move || -> rusqlite::Result<(u64, String)> {
        let c = app2.refdb.conn()?;
        Ok((c.generation(), run(&c, &app2, &q)?))
    })
    .await;
    match done {
        Ok(Ok((generation, body))) => {
            let body = Bytes::from(body);
            app.refdb.remember(key, generation, body.clone());
            json(body, CACHE)
        }
        Ok(Err(e)) => {
            eprintln!("search: {e}");
            ApiError::new(500, "internal_error", "internal error").into_response()
        }
        Err(e) => {
            eprintln!("search: {e}");
            ApiError::new(500, "internal_error", "internal error").into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn like_as_postgres() {
        assert!(like("SINGAPORE", "SIN%"));
        assert!(!like("SINGAPORE", "sin%"));
        assert!(like("SINGAPORE CHANGI", "% CHAN%"));
        assert!(!like("SINGAPORE", "% SING%"));
        assert!(like("A_B", "A\\_B"));
        assert!(!like("AXB", "A\\_B"));
        assert!(like("AXB", "A_B"));
        assert!(like("", "%"));
        assert!(like("ZÜRICH", "ZÜ%"));
        assert!(like("100%", "100\\%"));
    }

    #[test]
    fn edits() {
        assert_eq!(levenshtein("CHANGI", "CHNAGI"), 2);
        assert_eq!(levenshtein("HEATHROW", "HEATHRO"), 1);
        assert_eq!(levenshtein("", "ABC"), 3);
        assert!(near("SINGAPORE CHANGI AIRPORT SINGAPORE", "CHNAGI"));
        assert!(!near("OX", "OXO"));
    }

    #[test]
    fn shapes() {
        let s = shape("SQ322");
        assert!(s.flight && s.aircraft && !s.airport && !s.pair);
        let s = shape("SIN LHR");
        assert!(s.pair && !s.flight);
        let s = shape("SINGAPORE");
        assert!(s.airport && s.airline && !s.aircraft);
        assert_eq!(norm("  sin   lhr "), "SIN LHR");
        assert_eq!(norm("straße"), "STRASSE");
    }

    #[test]
    fn scores_keep_python_types() {
        assert_eq!(Score::Int(100).plus_int(2).rounded(), Score::Int(102));
        assert_eq!(Score::Int(60).plus(lift(999)).rounded(), Score::Float(72.0));
    }
}
