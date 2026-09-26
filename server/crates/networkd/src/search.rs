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
//!
//! What is typed is read first: a flight number typed with a space is
//! one number, two places joined by "to", "from", a dash or an arrow are
//! a route, and names compare without their accents (the table below,
//! the one Python folds with through Postgres's translate()). Any
//! spelling of the query other than its canonical one is answered with a
//! redirect to it, so the edge caches one answer per query.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, OnceLock};

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

/// Accents off, one letter for one letter, over what upper() left: the
/// same table as the Python service's (its tests hold the two equal).
const FOLD_FROM: &str = concat!(
    "ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝàáâã",
    "äåçèéêëìíîïñòóôõöùúûüýÿĀāĂăĄąĆ",
    "ćĈĉĊċČčĎďĒēĔĕĖėĘęĚěĜĝĞğĠġĢģĤĥĨ",
    "ĩĪīĬĭĮįİĴĵĶķĹĺĻļĽľŃńŅņŇňŌōŎŏŐő",
    "ŔŕŖŗŘřŚśŜŝŞşŠšŢţŤťŨũŪūŬŭŮůŰűŲų",
    "ŴŵŶŷŸŹźŻżŽžƠơƯưǍǎǏǐǑǒǓǔǕǖǗǘǙǚǛ",
    "ǜǞǟǠǡǦǧǨǩǪǫǬǭǰǴǵǸǹǺǻȀȁȂȃȄȅȆȇȈȉ",
    "ȊȋȌȍȎȏȐȑȒȓȔȕȖȗȘșȚțȞȟȦȧȨȩȪȫȬȭȮȯ",
    "ȰȱȲȳØøĐđŁłĦħŦŧı",
);
const FOLD_TO: &str = concat!(
    "AAAAAACEEEEIIIINOOOOOUUUUYAAAA",
    "AACEEEEIIIINOOOOOUUUUYYAAAAAAC",
    "CCCCCCCDDEEEEEEEEEEGGGGGGGGHHI",
    "IIIIIIIIJJKKLLLLLLNNNNNNOOOOOO",
    "RRRRRRSSSSSSSSTTTTUUUUUUUUUUUU",
    "WWYYYZZZZZZOOUUAAIIOOUUUUUUUUU",
    "UAAAAGGKKOOOOJGGNNAAAAAAEEEEII",
    "IIOOOORRRRUUUUSSTTHHAAEEOOOOOO",
    "OOYYOODDLLHHTTI",
);

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

fn fold(s: &str) -> String {
    static MAP: OnceLock<HashMap<char, char>> = OnceLock::new();
    let map = MAP.get_or_init(|| FOLD_FROM.chars().zip(FOLD_TO.chars()).collect());
    s.chars().map(|c| *map.get(&c).unwrap_or(&c)).collect()
}

/// `q` is one of the words of `text` (both folded).
fn whole(text: &Option<String>, q: &str) -> bool {
    text.as_deref().is_some_and(|t| !t.is_empty() && format!(" {t} ").contains(&format!(" {q} ")))
}

/// "SQ 322" -> "SQ322": an airline code of two or three letters and
/// digits (a letter among them), a space, then one to four digits and a
/// letter if the number has one. Anything else as it is.
fn compact_flight(q: &str) -> String {
    if let Some((code, number)) = q.split_once(' ') {
        let code_ok = (2..=3).contains(&code.len())
            && code.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
            && !code.chars().all(|c| c.is_ascii_digit());
        let digits = number.chars().take_while(|c| c.is_ascii_digit()).count();
        let rest = &number[digits..];
        let number_ok = (1..=4).contains(&digits) && (rest.is_empty() || (rest.len() == 1 && rest.chars().all(|c| c.is_ascii_uppercase())));
        if code_ok && number_ok {
            return format!("{code}{number}");
        }
    }
    q.to_string()
}

const ARROW: &str = "\u{2192}";

/// A route asked in words: (from, to). "SIN LHR", "SIN-LHR", "SIN → LHR",
/// "Singapore to London", "from SIN to LHR", "flights to London from
/// Singapore". A dash joins two places only between words of three
/// letters or more, so 9V-SMA stays a registration.
fn places(q: &str) -> Option<(String, String)> {
    let mut s = q.to_string();
    for a in ["->", ARROW, "\u{2013}", "\u{2014}", ">"] {
        s = s.replace(a, &format!(" {ARROW} "));
    }
    if let Some((left, right)) = s.split_once('-') {
        let lt: Vec<&str> = left.split(' ').filter(|t| !t.is_empty()).collect();
        let rt: Vec<&str> = right.split(' ').filter(|t| !t.is_empty()).collect();
        if !right.contains('-')
            && !lt.is_empty()
            && !rt.is_empty()
            && lt.iter().chain(rt.iter()).all(|t| is_alpha_word(t))
            && lt[lt.len() - 1].chars().count() >= 3
            && rt[0].chars().count() >= 3
        {
            s = format!("{left} {ARROW} {right}");
        }
    }
    let mut tokens: Vec<&str> = s.split(' ').filter(|t| !t.is_empty()).collect();
    if tokens.len() > 1 && tokens[0] == "FLIGHTS" {
        tokens.remove(0);
    }
    if tokens.is_empty() {
        return None;
    }
    let side = |ts: &[&str]| -> Option<String> {
        (!ts.is_empty() && !ts.iter().any(|t| ["TO", "FROM", ARROW].contains(t))).then(|| ts.join(" "))
    };
    let at = |w: &str| tokens.iter().position(|t| *t == w);
    let (a, b) = if let Some(i) = at(ARROW) {
        let mut left = &tokens[..i];
        if left.first() == Some(&"FROM") {
            left = &left[1..];
        }
        (side(left), side(&tokens[i + 1..]))
    } else if let (true, Some(i)) = (tokens[0] == "FROM", at("TO")) {
        (side(&tokens[1..i]), side(&tokens[i + 1..]))
    } else if let (true, Some(i)) = (tokens[0] == "TO", at("FROM")) {
        (side(&tokens[i + 1..]), side(&tokens[1..i]))
    } else if let Some(i) = at("TO") {
        (side(&tokens[..i]), side(&tokens[i + 1..]))
    } else if tokens.len() == 2 {
        (side(&tokens[..1]), side(&tokens[1..]))
    } else {
        (None, None)
    };
    Some((a?, b?))
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

fn fleet(c: &Conn, airlines: &[AirlineRow], q: &str) -> rusqlite::Result<Vec<Hit>> {
    let (a, b) = q.split_once(' ').unwrap();
    for (airline_q, type_q) in [(a, b), (b, a)] {
        // ORDER BY name LIMIT 1, the first of equal names in scan order
        let starts = format!("{}%", fold(airline_q));
        let airline = airlines
            .iter()
            .filter(|al| al.icao == airline_q || al.iata.as_deref() == Some(airline_q) || opt_like(&al.name_fold, &starts))
            .min_by_key(|al| al.rank);
        let Some(AirlineRow { icao, name, .. }) = airline else { continue };
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
        let mut params: Vec<&dyn ToSql> = vec![icao, &norm_like];
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
        // the prefix itself first, if it is a number, then the busiest
        let mut stmt = c.prepare_cached(
            "SELECT s.callsign, SUM(s.n_flights), MIN(s.org), MIN(s.dst), COUNT(*) FROM ref_schedule s \
             JOIN rank_callsign r ON r.key = s.callsign WHERE s.callsign LIKE ?1 ESCAPE '\\' \
             GROUP BY s.callsign ORDER BY s.callsign = ?2 DESC, 2 DESC, MIN(r.rank) LIMIT 5",
        )?;
        let rows: Vec<(String, i64, String, String, i64)> = stmt
            .query_map((format!("{p}%"), p), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)))?
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
                    // (base + lift) - 1, in Python's order of operations
                    score: Score::Int(base).plus(lift(n)).plus(-1.0),
                });
            }
        }
    }
    // every candidate scored, then the best five: BAW16 (exact) is not
    // cut by five busier BA16… rows read first
    out.sort_by(|a, b| b.score.value().total_cmp(&a.score.value()));
    out.truncate(PER_KIND);
    Ok(out)
}

/// A code, or a city or airport name, to one airport code; cities with
/// several airports resolve to the busiest.
/// A busy large airport answers for a word of its name too (Changi,
/// Heathrow).
fn airport_code(rows: &[AirportRow], word: &str) -> Option<String> {
    // ORDER BY exact first, traffic DESC LIMIT 1, over the scan's order
    let fw = fold(word);
    let starts = format!("{fw}%");
    let inner = format!("% {fw}%");
    let found: Vec<(usize, i64, i64)> = rows
        .iter()
        .enumerate()
        .filter_map(|(k, a)| {
            let exact = a.iata.as_deref() == Some(word) || a.ident == word;
            (exact
                || opt_like(&a.muni_fold, &starts)
                || opt_like(&a.name_fold, &starts)
                || (opt_like(&a.name_fold, &inner) && a.busy()))
            .then_some((k, if exact { 0 } else { 1 }, a.traffic))
        })
        .collect();
    let top = crate::pgsort::top_n(found, 1, &|x: &(usize, i64, i64), y: &(usize, i64, i64)| x.1.cmp(&y.1).then(y.2.cmp(&x.2)));
    top.first().map(|t| {
        let a = &rows[t.0];
        a.iata.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| a.ident.clone())
    })
}

fn route(c: &Conn, rows: &[AirportRow], a: &str, b: &str) -> rusqlite::Result<Vec<Hit>> {
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
    /// upper() (Postgres's) then folded
    name_fold: Option<String>,
    muni_fold: Option<String>,
    large: bool,
    traffic: i64,
    /// Postgres's order of names (nulls last)
    name_rank: i64,
}

impl AirportRow {
    /// A large airport the network sees flights leave: a word start in
    /// its name ranks as a prefix.
    fn busy(&self) -> bool {
        self.large && self.traffic > 0
    }
}

fn airport_rows(c: &Conn) -> rusqlite::Result<Vec<AirportRow>> {
    c.prepare(
        "SELECT a.ident, a.iata, a.name, a.municipality, a.iso_country, s.name_upper, s.municipality_upper, s.traffic, \
         coalesce(r.rank, 9223372036854775807), a.kind = 'large_airport' FROM ref_airports a JOIN search_airports s ON s.ident = a.ident \
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
            name_fold: r.get::<_, Option<String>>(5)?.map(|s| fold(&s)),
            muni_fold: r.get::<_, Option<String>>(6)?.map(|s| fold(&s)),
            traffic: r.get(7)?,
            name_rank: r.get(8)?,
            large: r.get(9)?,
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
/// AND levenshtein(w, q) <= edits` for some word.
fn near(text: &str, q: &str, edits: usize) -> bool {
    text.split(|c: char| c.is_whitespace()).any(|w| w.chars().count() >= 4 && levenshtein(w, q) <= edits)
}

/// The edits a near miss may take: one for four letters, two from five.
fn edits(q: &str) -> usize {
    if q.chars().count() == 4 {
        1
    } else {
        2
    }
}

/// (hits, whether `q` is a whole code or word of one of them)
fn airports(rows: &[AirportRow], q: &str) -> (Vec<Hit>, bool) {
    let fq = fold(q);
    let starts = format!("{fq}%");
    let word = format!("% {fq}%");
    // (row, class)
    let hits: Vec<(usize, i64)> = rows
        .iter()
        .enumerate()
        .filter_map(|(k, a)| {
            let exact = a.iata.as_deref() == Some(q) || a.ident == q;
            let start = opt_like(&a.name_fold, &starts) || opt_like(&a.muni_fold, &starts);
            let cls = if exact {
                EXACT
            } else if start {
                PREFIX
            } else if opt_like(&a.name_fold, &word) {
                if a.busy() {
                    PREFIX
                } else {
                    WORD
                }
            } else {
                return None;
            };
            Some((k, cls))
        })
        .collect();
    // ORDER BY cls DESC, traffic DESC, iata IS NULL, name LIMIT 5, as
    // Postgres picks among identical names
    let hits = crate::pgsort::top_n(hits, PER_KIND, &|x: &(usize, i64), y: &(usize, i64)| {
        let (a, b) = (&rows[x.0], &rows[y.0]);
        y.1.cmp(&x.1)
            .then(b.traffic.cmp(&a.traffic))
            .then(a.iata.is_none().cmp(&b.iata.is_none()))
            .then(a.name_rank.cmp(&b.name_rank))
    });
    let is_whole = hits.iter().any(|&(k, cls)| cls == EXACT || whole(&rows[k].name_fold, &fq) || whole(&rows[k].muni_fold, &fq));
    (hits.into_iter().map(|(k, cls)| airport_hit(&rows[k], cls)).collect(), is_whole)
}

/// A near miss: Chnagi, Heathro. ORDER BY traffic DESC LIMIT 5 as
/// Postgres picks among ties.
fn airports_near(rows: &[AirportRow], q: &str) -> Vec<Hit> {
    let (fq, n) = (fold(q), edits(q));
    let found: Vec<(usize, i64)> = rows
        .iter()
        .enumerate()
        .filter(|(_, a)| {
            let words = format!("{} {}", a.name_fold.as_deref().unwrap_or(""), a.muni_fold.as_deref().unwrap_or(""));
            near(&words, &fq, n)
        })
        .map(|(k, _)| (k, NEAR))
        .collect();
    crate::pgsort::top_n(found, PER_KIND, &|x: &(usize, i64), y: &(usize, i64)| rows[y.0].traffic.cmp(&rows[x.0].traffic))
        .into_iter()
        .map(|(k, cls)| airport_hit(&rows[k], cls))
        .collect()
}

fn airport_hit(a: &AirportRow, cls: i64) -> Hit {
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
}

/// The airlines, in storage order, with what the searches read: built
/// once per snapshot, scanned in memory.
struct AirlineRow {
    icao: String,
    iata: Option<String>,
    name: String,
    /// upper() (Postgres's) then folded
    name_fold: Option<String>,
    /// Postgres's order of names
    rank: i64,
}

fn airline_rows(c: &Conn) -> rusqlite::Result<Vec<AirlineRow>> {
    c.prepare(
        "SELECT al.icao, al.iata, al.name, s.name_upper, r.rank FROM ref_airlines al \
         JOIN search_airlines s ON s.icao = al.icao JOIN rank_ref_airlines_name r ON r.key = al.icao ORDER BY al.rowid",
    )?
    .query_map([], |r| {
        Ok(AirlineRow {
            icao: r.get(0)?,
            iata: r.get(1)?,
            name: r.get(2)?,
            name_fold: r.get::<_, Option<String>>(3)?.map(|s| fold(&s)),
            rank: r.get(4)?,
        })
    })?
    .collect()
}

/// (hits, whether `q` is a whole code or word of one of them)
fn airlines(rows: &[AirlineRow], q: &str) -> (Vec<Hit>, bool) {
    let fq = fold(q);
    let (starts, word) = (format!("{fq}%"), format!("% {fq}%"));
    // ORDER BY cls DESC, iata IS NULL, name LIMIT 5 over the scan's order
    let found: Vec<(usize, i64)> = rows
        .iter()
        .enumerate()
        .filter_map(|(k, al)| {
            let cls = if al.icao == q || al.iata.as_deref() == Some(q) {
                EXACT
            } else if opt_like(&al.name_fold, &starts) {
                PREFIX
            } else if opt_like(&al.name_fold, &word) {
                WORD
            } else {
                return None;
            };
            Some((k, cls))
        })
        .collect();
    let top = crate::pgsort::top_n(found, PER_KIND, &|x: &(usize, i64), y: &(usize, i64)| {
        let (a, b) = (&rows[x.0], &rows[y.0]);
        y.1.cmp(&x.1).then(a.iata.is_none().cmp(&b.iata.is_none())).then(a.rank.cmp(&b.rank))
    });
    let is_whole = top.iter().any(|&(k, cls)| cls == EXACT || whole(&rows[k].name_fold, &fq));
    (top.into_iter().map(|(k, cls)| airline_hit(&rows[k], cls)).collect(), is_whole)
}

/// ORDER BY name LIMIT 5 among near misses, as Postgres picks
fn airlines_near(rows: &[AirlineRow], q: &str) -> Vec<Hit> {
    let (fq, n) = (fold(q), edits(q));
    let found: Vec<(usize, i64)> = rows
        .iter()
        .enumerate()
        .filter(|(_, al)| al.name_fold.as_deref().is_some_and(|t| near(t, &fq, n)))
        .map(|(k, al)| (k, al.rank))
        .collect();
    crate::pgsort::top_n(found, PER_KIND, &|x: &(usize, i64), y: &(usize, i64)| x.1.cmp(&y.1))
        .into_iter()
        .map(|(k, _)| airline_hit(&rows[k], NEAR))
        .collect()
}

fn airline_hit(al: &AirlineRow, cls: i64) -> Hit {
    let iata = al.iata.clone().filter(|s| !s.is_empty());
    let detail = [Some(al.icao.clone()), iata.clone()].into_iter().flatten().collect::<Vec<_>>().join(" · ");
    Hit {
        kind: "airline",
        id: al.icao.clone(),
        label: al.name.clone(),
        detail: (!detail.is_empty()).then_some(detail),
        score: Score::Int(cls).plus_int(if iata.is_some() { 2 } else { 0 }),
    }
}

// ---- the route --------------------------------------------------------

fn run(c: &Conn, app: &App, q: &str) -> rusqlite::Result<String> {
    let term = compact_flight(q);
    let sh = shape(&term);
    let rows = app.refdb.memo("search_airport_rows", c, airport_rows)?;
    let carriers = app.refdb.memo("search_airline_rows", c, airline_rows)?;
    let mut results: Vec<Hit> = vec![];
    if let Some((a, b)) = places(&term) {
        results.extend(route(c, &rows, &a, &b)?);
    }
    if sh.pair {
        results.extend(fleet(c, &carriers, &term)?);
    }
    if sh.aircraft {
        results.extend(aircraft(c, &term)?);
    }
    if sh.flight {
        results.extend(flights(c, app, &term)?);
    }
    let (found_airports, whole_airport) = if sh.airport { airports(&rows, &term) } else { (vec![], false) };
    let (found_airlines, whole_airline) = if sh.airline { airlines(&carriers, &term) } else { (vec![], false) };
    let (no_airport, no_airline) = (found_airports.is_empty(), found_airlines.is_empty());
    results.extend(found_airports);
    results.extend(found_airlines);
    // near misses only when nothing matched a whole code or word: Scoot
    // the airline, not Scott AFB as well
    if term.chars().count() >= 4
        && is_alpha_word(&term)
        && !whole_airport
        && !whole_airline
        && !results.iter().any(|h| h.score.value() >= EXACT as f64)
    {
        if sh.airport && no_airport {
            results.extend(airports_near(&rows, &term));
        }
        if sh.airline && no_airline {
            results.extend(airlines_near(&carriers, &term));
        }
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

/// Python's `urllib.parse.quote(s, safe="")`.
fn quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// This request with q in its canonical spelling, every other parameter
/// as it came: 301, cached like the answer.
fn redirect(uri: &axum::http::Uri, q: &str) -> Response {
    let mut parts: Vec<String> = vec![];
    let mut placed = false;
    for piece in uri.query().unwrap_or("").split('&').filter(|p| !p.is_empty()) {
        let key = piece.split('=').next().unwrap_or("");
        if form_urlencoded::parse(key.as_bytes()).next().is_some_and(|(k, _)| k == "q") {
            if !placed {
                parts.push(format!("q={}", quote(q)));
                placed = true;
            }
            continue;
        }
        parts.push(piece.to_string());
    }
    let location = format!("{}?{}", uri.path(), parts.join("&"));
    let mut r = Response::new(axum::body::Body::empty());
    *r.status_mut() = axum::http::StatusCode::MOVED_PERMANENTLY;
    let h = r.headers_mut();
    if let Ok(v) = axum::http::HeaderValue::from_str(&location) {
        h.insert(axum::http::header::LOCATION, v);
    }
    h.insert(axum::http::header::CACHE_CONTROL, axum::http::HeaderValue::from_static(CACHE));
    r
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
    if raw != q {
        return redirect(req.uri(), &q);
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
    fn near_misses() {
        assert_eq!(levenshtein("CHANGI", "CHNAGI"), 2);
        assert_eq!(levenshtein("HEATHROW", "HEATHRO"), 1);
        assert_eq!(levenshtein("", "ABC"), 3);
        assert!(near("SINGAPORE CHANGI AIRPORT SINGAPORE", "CHNAGI", 2));
        assert!(!near("OX", "OXO", 2));
        // four letters take one edit, five and more two
        assert_eq!((edits("SCOT"), edits("SCOOT")), (1, 2));
        assert!(near("SCOOT", "SCOT", edits("SCOT")));
        assert!(!near("SCOTT AFB", "SCOO", edits("SCOO")));
    }

    #[test]
    fn folding() {
        assert_eq!(FOLD_FROM.chars().count(), FOLD_TO.chars().count());
        assert_eq!(fold("SÃO PAULO"), "SAO PAULO");
        assert_eq!(fold("ZÜRICH"), "ZURICH");
        assert_eq!(fold("WIDERØE"), "WIDEROE");
        assert_eq!(fold("KÖLN (COLOGNE)"), "KOLN (COLOGNE)");
        assert_eq!(fold("ßÆ"), "ßÆ");
        assert!(whole(&Some("SCOOT".into()), "SCOOT"));
        assert!(!whole(&Some("SCOTT AFB".into()), "SCOT"));
        assert!(whole(&Some("CHANGI AIR BASE (EAST)".into()), "AIR"));
    }

    #[test]
    fn flight_numbers_typed_with_a_space() {
        assert_eq!(compact_flight("SQ 322"), "SQ322");
        assert_eq!(compact_flight("SIA 322"), "SIA322");
        assert_eq!(compact_flight("BA 16A"), "BA16A");
        assert_eq!(compact_flight("6E 1"), "6E1");
        for q in ["737 800", "SIN LHR", "9V SMA", "SQ 32222", "SQ 322 X", "BA 16AB", "SQ322", "A 1"] {
            assert_eq!(compact_flight(q), q, "{q}");
        }
    }

    #[test]
    fn routes_in_words() {
        let r = |a: &str, b: &str| Some((a.to_string(), b.to_string()));
        for q in [
            "SIN LHR", "SIN-LHR", "SIN - LHR", "SIN\u{2013}LHR", "SIN \u{2014} LHR", "SIN \u{2192} LHR", "SIN>LHR", "SIN->LHR",
            "SIN TO LHR", "FROM SIN TO LHR", "TO LHR FROM SIN", "FROM SIN \u{2192} LHR", "FLIGHTS FROM SIN TO LHR",
        ] {
            assert_eq!(places(q), r("SIN", "LHR"), "{q}");
        }
        assert_eq!(places("FLIGHTS TO LONDON FROM SINGAPORE"), r("SINGAPORE", "LONDON"));
        assert_eq!(places("HONG KONG TO KUALA LUMPUR"), r("HONG KONG", "KUALA LUMPUR"));
        assert_eq!(places("TOKYO HANEDA"), r("TOKYO", "HANEDA"));
        for q in [
            "9V-SMA", "D-AIMA", "G-XLEA", "A6-EDA", "TOKYO", "TORONTO", "TO LONDON", "FROM SIN", "SIN TO", "FLIGHTS",
            "SIN TO LHR TO SYD", "A-B", "SIN-LHR-SYD", "\u{2192}", "SIN \u{2192} LHR \u{2192} SYD",
        ] {
            assert_eq!(places(q), None, "{q}");
        }
    }

    #[test]
    fn quote_as_python() {
        assert_eq!(quote("SQ 322"), "SQ%20322");
        assert_eq!(quote("SÃO PAULO"), "S%C3%83O%20PAULO");
        assert_eq!(quote("A-B_C.D~E/F&G=H+%"), "A-B_C.D~E%2FF%26G%3DH%2B%25");
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

    // ---- the route over a snapshot -------------------------------------

    use axum::body::Body;
    use tower::Service;

    fn snapshot(path: &std::path::Path) {
        let c = rusqlite::Connection::open(path).unwrap();
        c.execute_batch(
            "CREATE TABLE ref_airframes (hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, operator_name TEXT,
                 operator_norm TEXT, operator_icao TEXT);
             CREATE TABLE rank_ref_airframes_registration (key TEXT PRIMARY KEY, rank INTEGER NOT NULL);
             CREATE TABLE ref_types (designator TEXT PRIMARY KEY, name TEXT);
             CREATE TABLE search_types (designator TEXT PRIMARY KEY, name_upper TEXT);
             CREATE TABLE ref_airlines (icao TEXT PRIMARY KEY, iata TEXT, name TEXT);
             CREATE TABLE search_airlines (icao TEXT PRIMARY KEY, name_upper TEXT);
             CREATE TABLE rank_ref_airlines_name (key TEXT PRIMARY KEY, rank INTEGER NOT NULL);
             CREATE TABLE ref_airports (ident TEXT PRIMARY KEY, iata TEXT, name TEXT, municipality TEXT, iso_country TEXT, kind TEXT);
             CREATE TABLE search_airports (ident TEXT PRIMARY KEY, name_upper TEXT, municipality_upper TEXT, traffic INTEGER NOT NULL);
             CREATE TABLE rank_ref_airports_name (key TEXT PRIMARY KEY, rank INTEGER NOT NULL);
             CREATE TABLE ref_schedule (callsign TEXT, org TEXT, dst TEXT, n_flights INTEGER);
             CREATE TABLE rank_callsign (key TEXT PRIMARY KEY, rank INTEGER NOT NULL);

             INSERT INTO ref_airframes VALUES ('76cda1', '9V-SMA', 'A359', 'SINGAPORE AIRLINES', 'SINGAPORE AIRLINES', 'SIA');
             INSERT INTO rank_ref_airframes_registration VALUES ('76cda1', 0);
             INSERT INTO ref_types VALUES ('A359', 'Airbus A350-900');
             INSERT INTO search_types VALUES ('A359', 'AIRBUS A350-900');",
        )
        .unwrap();
        let airlines = [
            ("BAW", Some("BA"), "British Airways"),
            ("UAE", Some("EK"), "Emirates"),
            ("VSV", None, "SCAT Airlines"),
            ("TGW", Some("TR"), "Scoot"),
            ("SIA", Some("SQ"), "Singapore Airlines"),
            ("WIF", Some("WF"), "Widerøe"),
        ];
        for (k, (icao, iata, name)) in airlines.iter().enumerate() {
            c.execute("INSERT INTO ref_airlines VALUES (?1, ?2, ?3)", (icao, iata, name)).unwrap();
            c.execute("INSERT INTO search_airlines VALUES (?1, ?2)", (icao, name.to_uppercase())).unwrap();
            c.execute("INSERT INTO rank_ref_airlines_name VALUES (?1, ?2)", (icao, k as i64)).unwrap();
        }
        // (ident, iata, name, city, country, kind, traffic, name rank)
        let airports = [
            ("WSAC", None, "Changi Air Base (East)", "Singapore", "SG", "medium_airport", 280, 1),
            ("WSSS", Some("SIN"), "Singapore Changi Airport", "Singapore", "SG", "large_airport", 9000, 5),
            ("EGLL", Some("LHR"), "London Heathrow Airport", "London", "GB", "large_airport", 20000, 2),
            ("SBGR", Some("GRU"), "São Paulo/Guarulhos–Governor André Franco Montoro International Airport", "São Paulo", "BR",
             "large_airport", 5000, 3),
            ("KBLV", Some("BLV"), "Scott AFB/Midamerica Airport", "Belleville", "US", "medium_airport", 300, 4),
            ("YSSY", Some("SYD"), "Sydney Kingsford Smith International Airport", "Sydney", "AU", "large_airport", 8000, 6),
            ("OMDB", Some("DXB"), "Dubai International Airport", "Dubai", "AE", "large_airport", 30000, 0),
        ];
        for (ident, iata, name, city, country, kind, traffic, rank) in airports {
            c.execute("INSERT INTO ref_airports VALUES (?1, ?2, ?3, ?4, ?5, ?6)", (ident, iata, name, city, country, kind)).unwrap();
            c.execute("INSERT INTO search_airports VALUES (?1, ?2, ?3, ?4)", (ident, name.to_uppercase(), city.to_uppercase(), traffic))
                .unwrap();
            c.execute("INSERT INTO rank_ref_airports_name VALUES (?1, ?2)", (ident, rank)).unwrap();
        }
        let schedule = [
            ("SIA322", "SIN", "LHR", 46), ("SIA317", "LHR", "SIN", 40), ("BA1611", "MAD", "PHL", 5), ("BA1606", "MAD", "PHL", 4),
            ("BA1608", "BCN", "PHL", 4), ("BA1635", "FCO", "MIA", 4), ("BA1637", "MAD", "ORD", 4), ("BAW16", "SYD", "LHR", 62),
            ("UAE110", "DXB", "LHR", 55), ("UAE17K", "DXB", "LHR", 55), ("UAE19", "DXB", "MAN", 53), ("UAE11M", "DXB", "ZRH", 51),
            ("UAE185", "DXB", "BCN", 51), ("UAE1", "DXB", "LHR", 30),
        ];
        for (cs, org, dst, n) in schedule {
            c.execute("INSERT INTO ref_schedule VALUES (?1, ?2, ?3, ?4)", (cs, org, dst, n)).unwrap();
        }
        c.execute_batch(
            "INSERT INTO rank_callsign SELECT callsign, row_number() OVER (ORDER BY callsign) - 1
                 FROM (SELECT DISTINCT callsign FROM ref_schedule)",
        )
        .unwrap();
    }

    fn app(name: &str) -> Arc<App> {
        let d = std::env::temp_dir().join(format!("networkd-search-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        snapshot(&d.join("refdata.sqlite"));
        let mut s = crate::settings::Settings::from_env();
        s.refdata_path = d.join("refdata.sqlite").to_string_lossy().into();
        s.legs_path = d.join("legs.db").to_string_lossy().into();
        s.routes_path = d.join("routes.json.gz").to_string_lossy().into();
        s.gaps_path = d.join("gaps.json.gz").to_string_lossy().into();
        s.boards_path = d.join("boards.db").to_string_lossy().into();
        s.database_url = String::new();
        s.fallback = String::new();
        s.squawks = false;
        s.stations = false;
        s.estimates = false;
        s.nat = false;
        App::new(s)
    }

    async fn get(r: &mut axum::Router, path: &str) -> (u16, axum::http::HeaderMap, serde_json::Value) {
        let req = axum::http::Request::builder().uri(path).body(Body::empty()).unwrap();
        let resp = r.call(req).await.unwrap();
        let (status, headers) = (resp.status().as_u16(), resp.headers().clone());
        let bytes = axum::body::to_bytes(resp.into_body(), 1 << 20).await.unwrap();
        (status, headers, serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null))
    }

    async fn ids(r: &mut axum::Router, q: &str) -> Vec<String> {
        let (status, _, v) = get(r, &format!("/v1/search?q={}", quote(q))).await;
        assert_eq!(status, 200, "{q}");
        v["results"].as_array().unwrap().iter().map(|h| h["id"].as_str().unwrap().to_string()).collect()
    }

    #[tokio::test]
    async fn the_exact_number_is_scored_before_the_cap() {
        let mut r = crate::router(app("exact"));
        let got = ids(&mut r, "BA16").await;
        assert_eq!(got[0], "BAW16");
        assert_eq!(got.len(), 5);
        for q in ["UAE1", "EK1"] {
            assert_eq!(ids(&mut r, q).await[0], "UAE1", "{q}");
        }
    }

    #[tokio::test]
    async fn spaced_numbers_and_routes_in_words() {
        let mut r = crate::router(app("words"));
        for q in ["SQ 322", "SIA 322"] {
            assert_eq!(ids(&mut r, q).await, ["SIA322"], "{q}");
        }
        for q in [
            "SINGAPORE TO LONDON", "FROM SIN TO LHR", "TO LHR FROM SIN", "SIN-LHR", "SIN \u{2192} LHR", "SIN>LHR",
            "FLIGHTS TO LONDON FROM SINGAPORE", "CHANGI TO HEATHROW", "SINGAPORE - LONDON",
        ] {
            assert_eq!(ids(&mut r, q).await.first().map(String::as_str), Some("SIA322"), "{q}");
        }
        assert_eq!(ids(&mut r, "LONDON TO SINGAPORE").await[0], "SIA317");
        // a registration keeps its dash
        assert_eq!(ids(&mut r, "9V-SMA").await, ["76cda1"]);
    }

    #[tokio::test]
    async fn accents_rankings_and_near_misses() {
        let mut r = crate::router(app("rank"));
        assert_eq!(ids(&mut r, "SAO PAULO").await, ["GRU"]);
        assert_eq!(ids(&mut r, "WIDEROE").await, ["WIF"]);
        let (_, _, v) = get(&mut r, "/v1/search?q=SAO%20PAULO").await;
        assert_eq!(v["results"][0]["detail"], "São Paulo · BR");
        // the civil airport before the air base named first
        assert_eq!(ids(&mut r, "CHANGI").await, ["SIN", "WSAC"]);
        // Scoot matched whole: no near misses (Scott AFB) beside it
        assert_eq!(ids(&mut r, "SCOOT").await, ["TGW"]);
        // four letters, one edit: Scoot and SCAT beside Scott AFB's prefix
        let got = ids(&mut r, "SCOT").await;
        assert_eq!(got[0], "BLV");
        assert!(got.contains(&"TGW".to_string()) && got.contains(&"VSV".to_string()), "{got:?}");
        assert_eq!(ids(&mut r, "CHNAGI").await, ["SIN", "WSAC"]);
        assert_eq!(ids(&mut r, "EMIRATS").await, ["UAE"]);
    }

    #[tokio::test]
    async fn other_spellings_redirect_to_the_canonical_query() {
        let mut r = crate::router(app("redirect"));
        for (path, want) in [
            ("/v1/search?q=singapore", "/v1/search?q=SINGAPORE"),
            ("/v1/search?q=%20%20sq%20%20%20322%20", "/v1/search?q=SQ%20322"),
            ("/v1/search?q=S%C3%A3o+Paulo", "/v1/search?q=S%C3%83O%20PAULO"),
            ("/v1/search?x=1&q=sin&y=a+b", "/v1/search?x=1&q=SIN&y=a+b"),
            ("/v1/search?q=a&q=sin", "/v1/search?q=SIN"),
        ] {
            let (status, h, _) = get(&mut r, path).await;
            assert_eq!(status, 301, "{path}");
            assert_eq!(h["location"], want, "{path}");
            assert_eq!(h["cache-control"], CACHE);
        }
        let (status, h, v) = get(&mut r, "/v1/search?q=SQ%20322").await;
        assert_eq!((status, h["cache-control"].to_str().unwrap()), (200, CACHE));
        assert_eq!(v["q"], "SQ 322");
        let (status, _, _) = get(&mut r, "/v1/search?q=s").await;
        assert_eq!(status, 422);
    }
}
