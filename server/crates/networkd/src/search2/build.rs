//! `networkd search-index build`: the /v2 search index, written from the
//! nightly snapshot (refdata.sqlite) and, when given, the flight log
//! (legs.db).
//!
//! Each snapshot is one generation, named by its creation time
//! (`<out>/20260926T024000Z/`): the index files, lexicon.json (what
//! query understanding reads) and build.json (counts, timings). It is
//! written under a temporary name and renamed whole into place, then
//! `<out>/CURRENT` (the generation's name) is replaced by a rename, so a
//! reader never sees half an index. Generations but the current and the
//! one before it are removed. A generation already built is not built
//! again unless asked (--force).

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::Instant;

use anyhow::{bail, Context};
use chrono::TimeZone;
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::{json, Value};
use tantivy::{Index, TantivyDocument};

use super::lexicon::{table, Airline, Airport, City, Family, Lexicon, Type, CITIES, FAMILIES};
use super::schema::{schema, Fields};
use super::text::{compact, phrase, words};

const USAGE: &str = "usage: networkd search-index build --refdata <refdata.sqlite> [--legs <legs.db>] --out <dir> [--keep N] [--force]";
const HEAP: usize = 128 << 20;

pub struct Options {
    pub refdata: PathBuf,
    pub legs: Option<PathBuf>,
    pub out: PathBuf,
    pub keep: usize,
    pub force: bool,
}

/// The command line; exit status.
pub fn cli(args: &[String]) -> i32 {
    if args.first().map(String::as_str) != Some("build") {
        eprintln!("{USAGE}");
        return 2;
    }
    let mut o = Options { refdata: PathBuf::new(), legs: None, out: PathBuf::new(), keep: 2, force: false };
    let mut it = args[1..].iter();
    while let Some(a) = it.next() {
        let mut val = || it.next().cloned();
        match a.as_str() {
            "--refdata" => o.refdata = val().unwrap_or_default().into(),
            "--legs" => o.legs = val().map(Into::into),
            "--out" => o.out = val().unwrap_or_default().into(),
            "--keep" => o.keep = val().and_then(|v| v.parse().ok()).unwrap_or(0),
            "--force" => o.force = true,
            _ => {
                eprintln!("{USAGE}");
                return 2;
            }
        }
    }
    if o.refdata.as_os_str().is_empty() || o.out.as_os_str().is_empty() || o.keep < 1 {
        eprintln!("{USAGE}");
        return 2;
    }
    match build(&o) {
        Ok(report) => {
            println!("{report}");
            0
        }
        Err(e) => {
            eprintln!("search-index: {e:#}");
            1
        }
    }
}

/// "2026-09-26T02:40:00+00:00" -> "20260926T024000Z"
fn generation_of(created_at: &str) -> Option<String> {
    let t = chrono::DateTime::parse_from_rfc3339(created_at).ok()?.with_timezone(&chrono::Utc);
    Some(t.format("%Y%m%dT%H%M%SZ").to_string())
}

/// Build (or re-point to) the snapshot's generation; a one-line report.
pub fn build(o: &Options) -> anyhow::Result<String> {
    let started = Instant::now();
    let c = Connection::open_with_flags(&o.refdata, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("open {}", o.refdata.display()))?;
    let created: String = c
        .query_row("SELECT value FROM snapshot_meta WHERE key = 'created_at'", [], |r| r.get(0))
        .optional()?
        .context("snapshot_meta has no created_at")?;
    let generation = generation_of(&created).with_context(|| format!("created_at {created:?}"))?;
    let as_of = format!("{}Z", &chrono::DateTime::parse_from_rfc3339(&created)?.with_timezone(&chrono::Utc).format("%Y-%m-%dT%H:%M:%S"));
    std::fs::create_dir_all(&o.out)?;
    let dir = o.out.join(&generation);
    if dir.join("build.json").exists() && !o.force {
        point(&o.out, &generation)?;
        prune(&o.out, &generation, o.keep)?;
        return Ok(format!("search-index: {generation} already built; current"));
    }
    let tmp = o.out.join(format!(".build-{generation}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    std::fs::create_dir_all(&tmp)?;
    let (lexicon, counts) = write(&c, o.legs.as_deref(), &tmp, &generation, &as_of)?;
    std::fs::write(tmp.join("lexicon.json"), serde_json::to_vec(&lexicon)?)?;
    let bytes: u64 = std::fs::read_dir(&tmp)?.filter_map(|e| e.ok()?.metadata().ok()).map(|m| m.len()).sum();
    let secs = started.elapsed().as_secs_f64();
    let report = json!({"generation": generation, "as_of": as_of, "docs": counts, "bytes": bytes, "seconds": (secs * 10.0).round() / 10.0});
    std::fs::write(tmp.join("build.json"), serde_json::to_vec_pretty(&report)?)?;
    if dir.exists() {
        std::fs::remove_dir_all(&dir)?;
    }
    std::fs::rename(&tmp, &dir)?;
    point(&o.out, &generation)?;
    prune(&o.out, &generation, o.keep)?;
    Ok(format!("search-index: {generation} {} docs, {:.1} MB, {secs:.1} s", counts.values().sum::<u64>(), bytes as f64 / 1e6))
}

/// CURRENT names the generation readers open, replaced whole.
fn point(out: &Path, generation: &str) -> std::io::Result<()> {
    let tmp = out.join(format!(".CURRENT-{}", std::process::id()));
    std::fs::write(&tmp, format!("{generation}\n"))?;
    std::fs::rename(&tmp, out.join("CURRENT"))
}

/// Keep the current generation and the `keep - 1` newest before it;
/// leftovers of interrupted builds go too.
fn prune(out: &Path, current: &str, keep: usize) -> std::io::Result<()> {
    let mut gens = vec![];
    for e in std::fs::read_dir(out)? {
        let e = e?;
        let name = e.file_name().to_string_lossy().to_string();
        if name.starts_with(".build-") {
            // another build may be writing one; a day old, it was left
            let old = e.metadata()?.modified()?.elapsed().is_ok_and(|a| a.as_secs() > 86_400);
            if old || name.ends_with(&format!("-{}", std::process::id())) {
                std::fs::remove_dir_all(e.path())?;
            }
            continue;
        }
        if e.path().is_dir() && name.len() == 16 && name.ends_with('Z') {
            gens.push(name);
        }
    }
    gens.sort();
    let older: Vec<&String> = gens.iter().filter(|g| g.as_str() < current).collect();
    let keep_older = keep.saturating_sub(1);
    for g in older.iter().take(older.len().saturating_sub(keep_older)) {
        std::fs::remove_dir_all(out.join(g))?;
    }
    Ok(())
}

fn hhmm(m: Option<i64>) -> Value {
    m.map_or(Value::Null, |m| Value::String(format!("{:02}:{:02}", m.div_euclid(60), m.rem_euclid(60))))
}

/// Minutes from departure to arrival, each given in its airport's local
/// time, on `day` (the snapshot's date: its offsets).
fn block_min(day: chrono::NaiveDate, dep: Option<i64>, dep_tz: Option<&str>, arr: Option<i64>, arr_tz: Option<&str>) -> Option<i64> {
    let at = |m: i64, tz: &str| -> Option<i64> {
        let tz: chrono_tz::Tz = tz.parse().ok()?;
        let local = day.and_hms_opt((m / 60) as u32, (m % 60) as u32, 0)?;
        Some(tz.from_local_datetime(&local).earliest()?.timestamp() / 60)
    };
    let d = at(dep?, dep_tz?)?;
    let a = at(arr?, arr_tz?)?;
    let block = (a - d).rem_euclid(1440);
    (block > 0).then_some(block)
}

struct Place {
    ident: String,
    iata: Option<String>,
    name: Option<String>,
    city: Option<String>,
    country: Option<String>,
    kind: Option<String>,
    lat: Option<f64>,
    lon: Option<f64>,
    tz: Option<String>,
}

impl Place {
    fn code(&self) -> String {
        self.iata.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| self.ident.clone())
    }
}

struct Leg {
    org: String,
    dst: String,
    dep: Option<i64>,
    arr: Option<i64>,
    type_code: Option<String>,
    flight: Option<String>,
    source: Option<String>,
    n: i64,
}

/// A flight's legs in flying order: one chain when they make one, else
/// by departure time. Returns (route codes, legs in order, the ordered
/// pairs of every connected stretch).
fn chain(mut legs: Vec<Leg>) -> (Vec<String>, Vec<Leg>, Vec<String>) {
    legs.sort_by(|a, b| a.dep.unwrap_or(i64::MAX).cmp(&b.dep.unwrap_or(i64::MAX)).then(b.n.cmp(&a.n)));
    let dsts: HashSet<&str> = legs.iter().map(|l| l.dst.as_str()).collect();
    let orgs: HashSet<&str> = legs.iter().map(|l| l.org.as_str()).collect();
    let starts: Vec<usize> = (0..legs.len()).filter(|&k| !dsts.contains(legs[k].org.as_str())).collect();
    let mut order: Vec<usize> = vec![];
    if starts.len() == 1 && orgs.len() == legs.len() {
        let mut at = starts[0];
        loop {
            order.push(at);
            let next = legs[at].dst.clone();
            match (0..legs.len()).find(|&k| legs[k].org == next && !order.contains(&k)) {
                Some(k) => at = k,
                None => break,
            }
        }
    }
    if order.len() != legs.len() {
        order = (0..legs.len()).collect();
    }
    let mut slots: Vec<Option<Leg>> = legs.into_iter().map(Some).collect();
    let legs: Vec<Leg> = order.iter().map(|&k| slots[k].take().unwrap()).collect();
    let mut route: Vec<String> = vec![];
    let mut stretches: Vec<Vec<String>> = vec![];
    for l in &legs {
        if route.last() != Some(&l.org) {
            route.push(l.org.clone());
            stretches.push(vec![l.org.clone()]);
        }
        route.push(l.dst.clone());
        stretches.last_mut().unwrap().push(l.dst.clone());
    }
    let mut pairs = vec![];
    for s in &stretches {
        for i in 0..s.len() {
            for j in i + 1..s.len() {
                let p = format!("{}>{}", s[i], s[j]);
                if s[i] != s[j] && !pairs.contains(&p) {
                    pairs.push(p);
                }
            }
        }
    }
    (route, legs, pairs)
}

/// Manufacturers whose names are more than one word.
const MAKERS: &[&str] = &["De Havilland Canada", "De Havilland", "McDonnell Douglas", "British Aerospace", "Sukhoi Superjet"];

fn manufacturer(name: &str) -> Option<String> {
    for m in MAKERS {
        if name.starts_with(m) {
            return Some(m.to_string());
        }
    }
    name.split_whitespace().next().map(str::to_string)
}

struct Writer<'a> {
    w: tantivy::IndexWriter,
    f: Fields,
    counts: &'a mut std::collections::BTreeMap<String, u64>,
}

impl Writer<'_> {
    #[allow(clippy::too_many_arguments)]
    fn add(
        &mut self,
        kind: &str,
        codes: &[String],
        text: &[String],
        pairs: &[String],
        airports: &[String],
        airline: Option<&str>,
        types: &[String],
        pop: i64,
        doc: &Value,
    ) -> anyhow::Result<()> {
        let f = self.f;
        let mut d = TantivyDocument::default();
        d.add_text(f.kind, kind);
        let mut seen = HashSet::new();
        for c in codes.iter().filter(|c| !c.is_empty()) {
            if seen.insert(c.as_str()) {
                d.add_text(f.codes, c);
            }
        }
        if !text.is_empty() {
            d.add_text(f.text, text.join(" "));
        }
        for p in pairs {
            d.add_text(f.pairs, p);
        }
        for a in airports {
            d.add_text(f.airports, a);
        }
        if let Some(a) = airline {
            d.add_text(f.airline, a);
        }
        for t in types {
            d.add_text(f.types, t);
        }
        d.add_u64(f.pop, pop.max(0) as u64);
        d.add_bytes(f.doc, &serde_json::to_vec(doc)?);
        self.w.add_document(d)?;
        *self.counts.entry(kind.to_string()).or_default() += 1;
        Ok(())
    }
}

fn opt(s: &Option<String>) -> Value {
    s.clone().filter(|s| !s.is_empty()).map_or(Value::Null, Value::String)
}

type Counts = std::collections::BTreeMap<String, u64>;

fn write(c: &Connection, legs_db: Option<&Path>, dir: &Path, generation: &str, as_of: &str) -> anyhow::Result<(Lexicon, Counts)> {
    let (schema, f) = schema();
    let index = Index::create_in_dir(dir, schema)?;
    let mut counts = Counts::new();
    let mut w = Writer { w: index.writer_with_num_threads(1, HEAP)?, f, counts: &mut counts };
    let day = chrono::NaiveDate::parse_from_str(&as_of[..10], "%Y-%m-%d")?;

    // ---- the flight log: last seen, and callsigns only it knows ------
    // kept only for the callsigns the index holds: the schedule's, and
    // an airline's numbered callsigns the schedule lacks
    let scheduled: HashSet<String> =
        c.prepare("SELECT DISTINCT callsign FROM ref_schedule")?.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    let designators: HashSet<String> =
        c.prepare("SELECT icao FROM ref_airlines")?.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
    let numbered = |cs: &str| {
        cs.len() > 3 && cs.is_char_boundary(3) && designators.contains(&cs[..3]) && cs[3..].starts_with(|c: char| c.is_ascii_digit())
    };
    let mut seen: HashMap<String, (i64, Option<String>)> = HashMap::new();
    let mut window_days = None;
    // callsign -> its most flown leg (org, dst, flights)
    let mut log_legs: HashMap<String, (String, String, i64)> = HashMap::new();
    if let Some(p) = legs_db {
        let l = Connection::open_with_flags(p, OpenFlags::SQLITE_OPEN_READ_ONLY).with_context(|| format!("open {}", p.display()))?;
        window_days = l
            .query_row("SELECT value FROM meta WHERE key = 'window_days'", [], |r| r.get::<_, Option<String>>(0))
            .optional()?
            .flatten()
            .and_then(|v| v.parse().ok());
        let mut stmt = l.prepare("SELECT callsign, org, dst, COUNT(*), MAX(date) FROM legs WHERE callsign IS NOT NULL GROUP BY callsign, org, dst")?;
        let mut rows = stmt.query([])?;
        while let Some(r) = rows.next()? {
            let cs: String = r.get(0)?;
            let known = scheduled.contains(&cs);
            if !known && !numbered(&cs) {
                continue;
            }
            let (org, dst): (Option<String>, Option<String>) = (r.get(1)?, r.get(2)?);
            let n: i64 = r.get(3)?;
            let last: Option<String> = r.get(4)?;
            let e = seen.entry(cs.clone()).or_insert((0, None));
            e.0 += n;
            if last > e.1 {
                e.1 = last;
            }
            if let (false, Some(o), Some(d)) = (known, org, dst) {
                let best = log_legs.entry(cs).or_insert((o.clone(), d.clone(), 0));
                if n > best.2 {
                    *best = (o, d, n);
                }
            }
        }
    }

    // ---- airlines ----------------------------------------------------
    let mut flights_by_airline: HashMap<String, i64> = HashMap::new();
    for r in c.prepare("SELECT airline_icao, SUM(n_flights) FROM ref_schedule GROUP BY airline_icao")?.query_map([], |r| {
        Ok((r.get::<_, Option<String>>(0)?, r.get::<_, i64>(1)?))
    })? {
        let (a, n) = r?;
        if let Some(a) = a {
            flights_by_airline.insert(a, n);
        }
    }
    let mut airlines: Vec<Airline> = vec![];
    for r in c.prepare("SELECT icao, iata, name FROM ref_airlines ORDER BY rowid")?.query_map([], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, Option<String>>(2)?))
    })? {
        let (icao, iata, name) = r?;
        let name = name.unwrap_or_else(|| icao.clone());
        let flights = flights_by_airline.get(&icao).copied().unwrap_or(0);
        airlines.push(Airline { icao, iata: iata.filter(|s| !s.is_empty()), name, flights });
    }
    let airline_at: HashMap<String, usize> = airlines.iter().enumerate().map(|(k, a)| (a.icao.clone(), k)).collect();
    for a in &airlines {
        let doc = json!({
            "kind": "airline", "id": a.icao, "icao": a.icao, "iata": a.iata, "name": a.name,
            "flights": a.flights, "url": format!("airline.html?icao={}", a.icao),
            "_m": [phrase(&a.name)],
        });
        let codes: Vec<String> = std::iter::once(a.icao.clone()).chain(a.iata.clone()).collect();
        w.add("airline", &codes, &words(&a.name), &[], &[], Some(&a.icao), &[], a.flights, &doc)?;
    }

    // ---- airports ----------------------------------------------------
    let mut traffic: HashMap<String, i64> = HashMap::new();
    for r in c.prepare("SELECT org, SUM(n_flights) FROM ref_schedule GROUP BY org")?.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)))? {
        let (k, n) = r?;
        traffic.insert(k, n);
    }
    let mut places: Vec<Place> = vec![];
    for r in c
        .prepare("SELECT ident, iata, name, municipality, iso_country, kind, lat, lon, tz FROM ref_airports ORDER BY rowid")?
        .query_map([], |r| {
            Ok(Place {
                ident: r.get(0)?,
                iata: r.get::<_, Option<String>>(1)?.filter(|s| !s.is_empty()),
                name: r.get(2)?,
                city: r.get(3)?,
                country: r.get(4)?,
                kind: r.get(5)?,
                lat: r.get(6)?,
                lon: r.get(7)?,
                tz: r.get(8)?,
            })
        })?
    {
        places.push(r?);
    }
    let mut by_code: HashMap<String, usize> = HashMap::new();
    for (k, p) in places.iter().enumerate() {
        by_code.entry(p.ident.clone()).or_insert(k);
    }
    for (k, p) in places.iter().enumerate() {
        // the IATA code wins over another airport's ident
        if let Some(i) = &p.iata {
            by_code.insert(i.clone(), k);
        }
    }
    let indexed = |p: &Place| {
        let large_or_medium = matches!(p.kind.as_deref(), Some("large_airport" | "medium_airport"));
        large_or_medium || traffic.get(&p.code()).copied().unwrap_or(0) > 0
    };
    // the cities table, over the airports the snapshot has
    let mut cities: Vec<City> = vec![];
    let mut city_names: HashMap<String, Vec<String>> = HashMap::new();
    for (name, others, codes) in table(CITIES) {
        let codes: Vec<String> = codes.into_iter().filter(|x| by_code.get(x).is_some_and(|&k| places[k].code() == *x)).collect();
        if codes.is_empty() {
            continue;
        }
        for code in &codes {
            let e = city_names.entry(code.clone()).or_default();
            e.push(name.clone());
            e.extend(others.iter().cloned());
        }
        cities.push(City { name, names: others, airports: codes });
    }
    let mut lex_airports = vec![];
    for p in places.iter().filter(|p| indexed(p)) {
        let code = p.code();
        let t = traffic.get(&code).copied().unwrap_or(0);
        let also = city_names.get(&code).cloned().unwrap_or_default();
        let mut m: Vec<String> = vec![];
        for s in p.name.iter().chain(p.city.iter()).chain(also.iter()) {
            let ph = phrase(s);
            if !ph.is_empty() && !m.contains(&ph) {
                m.push(ph);
            }
        }
        let text: Vec<String> = m.iter().flat_map(|s| s.split(' ').map(str::to_string)).collect();
        let ll = match (p.lat, p.lon) {
            (Some(a), Some(b)) => json!([a, b]),
            _ => Value::Null,
        };
        let doc = json!({
            "kind": "airport", "id": code, "iata": p.iata, "icao": p.ident, "name": opt(&p.name),
            "city": opt(&p.city), "country": opt(&p.country), "tz": opt(&p.tz),
            "flights": t, "url": format!("?airport={code}"),
            "_m": m, "_ll": ll,
        });
        let codes: Vec<String> = std::iter::once(p.ident.clone()).chain(p.iata.clone()).collect();
        w.add("airport", &codes, &text, &[], &[code.clone()], None, &[], t, &doc)?;
        lex_airports.push(Airport {
            code,
            ident: p.ident.clone(),
            name: p.name.clone(),
            city: p.city.clone(),
            traffic: t,
            large: p.kind.as_deref() == Some("large_airport"),
        });
    }

    // ---- types and families -------------------------------------------
    let mut airframes_of: HashMap<String, i64> = HashMap::new();
    for r in c.prepare("SELECT type_code, COUNT(*) FROM ref_airframes WHERE type_code IS NOT NULL GROUP BY type_code")?.query_map([], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
    })? {
        let (t, n) = r?;
        airframes_of.insert(t, n);
    }
    let mut type_names: HashMap<String, String> = HashMap::new();
    for r in c.prepare("SELECT designator, name FROM ref_types ORDER BY rowid")?.query_map([], |r| {
        Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
    })? {
        let (d, n) = r?;
        if let Some(n) = n {
            type_names.insert(d, n);
        }
    }
    let mut families = vec![];
    let mut family_of: HashMap<String, (String, Vec<String>)> = HashMap::new();
    for (name, others, codes) in table(FAMILIES) {
        let mut ds: Vec<String> = codes.into_iter().filter(|d| type_names.contains_key(d) || airframes_of.contains_key(d)).collect();
        // most airframes first, the table's order among equals
        ds.sort_by_key(|d| std::cmp::Reverse(airframes_of.get(d).copied().unwrap_or(0)));
        if ds.is_empty() {
            continue;
        }
        for d in &ds {
            // a designator's family is the first (widest) row naming it
            family_of.entry(d.clone()).or_insert_with(|| (name.clone(), others.clone()));
        }
        families.push(Family { name, names: others, designators: ds });
    }
    let mut designators: Vec<&String> = type_names.keys().chain(family_of.keys()).collect();
    designators.sort();
    designators.dedup();
    let mut lex_types = vec![];
    for d in designators {
        let name = type_names.get(d).cloned();
        let n = airframes_of.get(d).copied().unwrap_or(0);
        let fam = family_of.get(d);
        let mut m: Vec<String> = vec![d.to_lowercase()];
        for s in name.iter().chain(fam.iter().flat_map(|(n, o)| std::iter::once(n).chain(o.iter()))) {
            let ph = phrase(s);
            if !m.contains(&ph) {
                m.push(ph);
            }
        }
        let text: Vec<String> = m.iter().flat_map(|s| s.split(' ').map(str::to_string)).collect();
        let doc = json!({
            "kind": "type", "id": d, "designator": d, "name": name, "manufacturer": name.as_deref().and_then(manufacturer),
            "family": fam.map(|f| f.0.clone()), "airframes": n, "url": Value::Null,
            "_m": m,
        });
        w.add("type", &[d.clone()], &text, &[], &[], None, &[d.clone()], n, &doc)?;
        lex_types.push(Type { designator: d.clone(), name, airframes: n });
    }

    // ---- flights -----------------------------------------------------
    let tz_of = |code: &str| by_code.get(code).and_then(|&k| places[k].tz.clone());
    let city_of = |code: &str| by_code.get(code).and_then(|&k| places[k].city.clone());
    let mut stmt = c.prepare(
        "SELECT callsign, org, dst, airline_icao, dep_min, arr_min, type_code, flight, source, n_flights \
         FROM ref_schedule ORDER BY callsign, n_flights DESC",
    )?;
    let mut rows = stmt.query([])?;
    let mut group: Vec<Leg> = vec![];
    let mut current: Option<(String, Option<String>)> = None;
    let emit = |w: &mut Writer, cs: &str, airline: Option<&str>, legs: Vec<Leg>| -> anyhow::Result<()> {
        let flights: i64 = legs.iter().map(|l| l.n).sum();
        let al = airline.and_then(|a| airline_at.get(a)).map(|&k| &airlines[k]);
        // the marketed number a board named, else the airline's IATA code
        // before the callsign's number
        let mut named: Vec<(String, i64)> = legs.iter().filter_map(|l| l.flight.clone().map(|f| (f, l.n))).collect();
        named.sort_by(|a, b| b.1.cmp(&a.1));
        let derived = al.and_then(|a| {
            let rest = cs.strip_prefix(a.icao.as_str())?;
            (rest.starts_with(|c: char| c.is_ascii_digit())).then(|| a.iata.as_ref().map(|i| format!("{i}{rest}")))?
        });
        let marketed = named.first().map(|f| f.0.clone()).or(derived.clone());
        let (route, legs, pairs) = chain(legs);
        let cities: Vec<Value> = route.iter().map(|c| city_of(c).map_or(Value::Null, Value::String)).collect();
        let mut types: Vec<String> = vec![];
        let legs_json: Vec<Value> = legs
            .iter()
            .map(|l| {
                if let Some(t) = &l.type_code {
                    if !types.contains(t) {
                        types.push(t.clone());
                    }
                }
                let (dt, at) = (tz_of(&l.org), tz_of(&l.dst));
                json!({
                    "org": l.org, "dst": l.dst, "dep": hhmm(l.dep), "arr": hhmm(l.arr),
                    "dep_tz": dt, "arr_tz": at,
                    "block_min": block_min(day, l.dep, dt.as_deref(), l.arr, at.as_deref()),
                    "type": l.type_code, "times": l.source, "flights": l.n,
                })
            })
            .collect();
        let (log_n, last_seen) = seen.get(cs).cloned().unwrap_or((0, None));
        let doc = json!({
            "kind": "flight", "id": cs, "callsign": cs, "flight": marketed,
            "airline": al.map(|a| json!({"icao": a.icao, "iata": a.iata, "name": a.name})),
            "route": route, "cities": cities, "legs": legs_json,
            "flights": if flights > 0 { flights } else { log_n },
            "window_days": window_days, "last_seen": last_seen,
            "url": format!("flight.html?callsign={cs}"),
        });
        let mut codes = vec![cs.to_string()];
        codes.extend(named.iter().map(|f| f.0.clone()));
        codes.extend(derived);
        w.add("flight", &codes, &[], &pairs, &route, al.map(|a| a.icao.as_str()), &types, flights.max(log_n), &doc)
    };
    while let Some(r) = rows.next()? {
        let cs: String = r.get(0)?;
        let airline: Option<String> = r.get(3)?;
        if current.as_ref().map(|c| &c.0) != Some(&cs) {
            if let Some((prev, al)) = current.take() {
                emit(&mut w, &prev, al.as_deref(), std::mem::take(&mut group))?;
            }
            current = Some((cs.clone(), airline));
        }
        group.push(Leg {
            org: r.get(1)?,
            dst: r.get(2)?,
            dep: r.get(4)?,
            arr: r.get(5)?,
            type_code: r.get(6)?,
            flight: r.get(7)?,
            source: r.get(8)?,
            n: r.get(9)?,
        });
    }
    if let Some((prev, al)) = current.take() {
        emit(&mut w, &prev, al.as_deref(), std::mem::take(&mut group))?;
    }
    // flights only the log knows: an airline's callsign seen at least
    // twice, on its most flown leg
    let mut only: Vec<(&String, &(String, String, i64))> = log_legs.iter().filter(|(cs, _)| !scheduled.contains(*cs)).collect();
    only.sort_by(|a, b| a.0.cmp(b.0));
    for (cs, (org, dst, _)) in only {
        let total = seen.get(cs).map_or(0, |s| s.0);
        if total < 2 {
            continue;
        }
        let leg = Leg { org: org.clone(), dst: dst.clone(), dep: None, arr: None, type_code: None, flight: None, source: Some("observed".into()), n: 0 };
        emit(&mut w, cs, Some(&cs[..3]), vec![leg])?;
    }

    // ---- airframes ---------------------------------------------------
    let mut stmt = c.prepare("SELECT hex, registration, type_code, operator_name, operator_icao FROM ref_airframes ORDER BY rowid")?;
    let mut rows = stmt.query([])?;
    while let Some(r) = rows.next()? {
        let hex: String = r.get(0)?;
        let reg: Option<String> = r.get::<_, Option<String>>(1)?.filter(|s| !s.is_empty());
        let t: Option<String> = r.get(2)?;
        let op_name: Option<String> = r.get(3)?;
        let op: Option<String> = r.get(4)?;
        let al = op.as_ref().and_then(|o| airline_at.get(o)).map(|&k| &airlines[k]);
        let doc = json!({
            "kind": "aircraft", "id": hex, "hex": hex, "reg": reg, "type": t,
            "type_name": t.as_ref().and_then(|t| type_names.get(t)),
            "operator": al.map(|a| a.name.clone()).or(op_name.filter(|s| !s.is_empty())),
            "operator_icao": op,
            "url": format!("plane.html?hex={hex}"),
        });
        let mut codes = vec![hex.to_uppercase()];
        if let Some(r) = &reg {
            codes.push(r.to_uppercase());
            codes.push(compact(r));
        }
        let types: Vec<String> = t.iter().cloned().collect();
        w.add("aircraft", &codes, &[], &[], &[], op.as_deref(), &types, i64::from(op.is_some()), &doc)?;
    }

    // one segment: fewer lookups per query
    let mut iw = w.w;
    iw.commit()?;
    let ids = index.searchable_segment_ids()?;
    if ids.len() > 1 {
        iw.merge(&ids).wait()?;
    }
    iw.wait_merging_threads()?;
    // the writer's lock files: nothing writes a generation again
    for e in std::fs::read_dir(dir)? {
        let p = e?.path();
        if p.file_name().is_some_and(|n| n.to_string_lossy().ends_with(".lock")) {
            std::fs::remove_file(p)?;
        }
    }
    let lexicon = Lexicon {
        generation: generation.to_string(),
        as_of: as_of.to_string(),
        window_days,
        airlines,
        airports: lex_airports,
        cities,
        families,
        types: lex_types,
    };
    if counts.values().sum::<u64>() == 0 {
        bail!("the snapshot holds nothing to index");
    }
    Ok((lexicon, counts))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn leg(org: &str, dst: &str, dep: Option<i64>) -> Leg {
        Leg { org: org.into(), dst: dst.into(), dep, arr: None, type_code: None, flight: None, source: None, n: 1 }
    }

    #[test]
    fn legs_in_flying_order() {
        let (route, _, pairs) = chain(vec![leg("SIN", "LHR", Some(900)), leg("SYD", "SIN", Some(300))]);
        assert_eq!(route, ["SYD", "SIN", "LHR"]);
        assert_eq!(pairs, ["SYD>SIN", "SYD>LHR", "SIN>LHR"]);
        // no chain: by departure, each leg its own stretch
        let (route, _, pairs) = chain(vec![leg("MAD", "PHL", Some(600)), leg("BCN", "PHL", Some(500))]);
        assert_eq!(route, ["BCN", "PHL", "MAD", "PHL"]);
        assert_eq!(pairs, ["BCN>PHL", "MAD>PHL"]);
        // there and back
        let (route, _, _) = chain(vec![leg("SIN", "KUL", Some(600)), leg("KUL", "SIN", Some(900))]);
        assert_eq!(route, ["SIN", "KUL", "SIN"]);
    }

    #[test]
    fn block_minutes_across_zones() {
        let day = chrono::NaiveDate::from_ymd_opt(2026, 9, 26).unwrap();
        // SIN 23:35 -> LHR 06:25 next morning: 13 h 50
        assert_eq!(block_min(day, Some(23 * 60 + 35), Some("Asia/Singapore"), Some(6 * 60 + 25), Some("Europe/London")), Some(830));
        assert_eq!(block_min(day, Some(600), None, Some(700), Some("Europe/London")), None);
        assert_eq!(generation_of("2026-09-26T02:40:00+00:00").as_deref(), Some("20260926T024000Z"));
        assert_eq!(manufacturer("Airbus A350-900").as_deref(), Some("Airbus"));
        assert_eq!(manufacturer("De Havilland Canada Dash 8").as_deref(), Some("De Havilland Canada"));
    }
}
