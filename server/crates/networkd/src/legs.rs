//! The per-airframe legs artifact (SQLite, read-only, written nightly):
//! its edge (`archive_through`, for /v1/now), its window, and each
//! airport's observed totals.
//!
//! The file is re-checked every five minutes and reopened when it
//! changes; a missing file means the archive is dark. Airport totals are
//! a scan of that airport's legs (LHR: ~150k of them), so each is
//! computed once per file and kept; a background pass warms every
//! airport after a new file loads, so no visitor waits on the scan. That
//! pass reads the table once in storage order: walking the index airport
//! by airport re-read the file dozens of times over, from disk wherever
//! the file outgrows the page cache the container may keep.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime};

use rusqlite::{Connection, OpenFlags};

use crate::pyjson::{write_str, Obj};

const RELOAD: Duration = Duration::from_secs(300);

struct Inner {
    loaded_mtime: Option<SystemTime>,
    next_check: Option<Instant>,
    through: Option<String>,
    window_days: Option<i64>,
    conn: Option<Connection>,
    /// airport code -> `observed` JSON (None: no departures observed)
    airports: HashMap<String, Option<Arc<str>>>,
    warming: bool,
}

pub struct LegBook {
    path: String,
    inner: Mutex<Inner>,
}

fn open(path: &str) -> rusqlite::Result<Connection> {
    let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    c.query_row("SELECT 1 FROM legs LIMIT 1", [], |_| Ok(()))?;
    Ok(c)
}

/// One airport's observed totals and busiest routes, as `legs_db.py`
/// computes them (circuits excluded); None when nothing departed it.
fn airport_json(c: &Connection, code: &str) -> rusqlite::Result<Option<Arc<str>>> {
    let (n, dsts, tails, days): (i64, i64, i64, i64) = c.prepare_cached(
        "SELECT COUNT(*), COUNT(DISTINCT dst), COUNT(DISTINCT hex), COUNT(DISTINCT date) \
         FROM legs WHERE org = ? AND (dst IS NULL OR dst <> org)",
    )?
    .query_row([code], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?;
    if n == 0 {
        return Ok(None);
    }
    let mut out = String::with_capacity(1024);
    let mut o = Obj::new(&mut out);
    o.int("departures", n).int("destinations", dsts).int("tails", tails).int("days_observed", days);
    let buf = o.key("routes");
    buf.push('[');
    let mut stmt = c.prepare_cached(
        "SELECT dst, COUNT(*), COUNT(DISTINCT date) FROM legs WHERE org = ? AND dst IS NOT NULL \
         AND dst <> org GROUP BY dst ORDER BY 2 DESC LIMIT 15",
    )?;
    let mut rows = stmt.query([code])?;
    let mut first = true;
    while let Some(r) = rows.next()? {
        if !first {
            buf.push(',');
        }
        first = false;
        let dst: String = r.get(0)?;
        let mut item = Obj::new(buf);
        write_str(item.key("dst"), &dst);
        item.int("flights", r.get(1)?).int("days", r.get(2)?);
        item.end();
    }
    buf.push(']');
    o.end();
    Ok(Some(out.into()))
}

/// Every airport's `airport_json`, from one pass over the table in
/// storage order instead of one index walk per airport: the index walks
/// fetch rows all over the file, and where the file does not fit in
/// the page cache each fetch is a disk read. Same answers (the test
/// below holds them to airport_json), busiest routes tied on flights
/// in reverse code order, as SQLite leaves them (its GROUP BY hands
/// the routes over in code order, its ORDER BY ... LIMIT puts the later
/// of two ties first).
/// `keep_going` is asked now and then; false abandons the pass.
fn all_airports(c: &Connection, keep_going: &dyn Fn() -> bool) -> rusqlite::Result<Option<HashMap<String, Option<Arc<str>>>>> {
    use std::collections::HashSet;
    #[derive(Default)]
    struct Agg {
        n: i64,
        dsts: HashSet<u32>,
        tails: HashSet<u32>,
        days: HashSet<u32>,
        /// dst -> (flights, days)
        routes: HashMap<u32, (i64, HashSet<u32>)>,
    }
    // strings to small ids: codes and dates for the sets, hexes too
    let mut names: HashMap<String, u32> = HashMap::new();
    let mut by_id: Vec<String> = vec![];
    let mut intern = |s: String| -> u32 {
        if let Some(&i) = names.get(&s) {
            return i;
        }
        let i = by_id.len() as u32;
        by_id.push(s.clone());
        names.insert(s, i);
        i
    };
    let mut aggs: HashMap<u32, Agg> = HashMap::new();
    let mut stmt = c.prepare("SELECT org, dst, hex, date FROM legs")?;
    let mut rows = stmt.query([])?;
    let mut seen = 0u64;
    while let Some(r) = rows.next()? {
        seen += 1;
        if seen.is_multiple_of(1_000_000) && !keep_going() {
            return Ok(None);
        }
        let Some(org) = r.get::<_, Option<String>>(0)? else { continue };
        let dst: Option<String> = r.get(1)?;
        if dst.as_deref() == Some(org.as_str()) {
            continue; // circuits are not departures
        }
        let hex: Option<String> = r.get(2)?;
        let date: Option<String> = r.get(3)?;
        let org = intern(org);
        let dst = dst.map(&mut intern);
        let hex = hex.map(&mut intern);
        let date = date.map(&mut intern);
        let a = aggs.entry(org).or_default();
        a.n += 1;
        if let Some(d) = dst {
            a.dsts.insert(d);
            let route = a.routes.entry(d).or_default();
            route.0 += 1;
            if let Some(day) = date {
                route.1.insert(day);
            }
        }
        if let Some(h) = hex {
            a.tails.insert(h);
        }
        if let Some(day) = date {
            a.days.insert(day);
        }
    }
    let mut out = HashMap::new();
    for (org, a) in aggs {
        let mut routes: Vec<(&str, i64, usize)> =
            a.routes.iter().map(|(d, (n, days))| (by_id[*d as usize].as_str(), *n, days.len())).collect();
        routes.sort_by(|x, y| y.1.cmp(&x.1).then_with(|| y.0.cmp(x.0)));
        let mut s = String::with_capacity(1024);
        let mut o = Obj::new(&mut s);
        o.int("departures", a.n)
            .int("destinations", a.dsts.len() as i64)
            .int("tails", a.tails.len() as i64)
            .int("days_observed", a.days.len() as i64);
        let buf = o.key("routes");
        buf.push('[');
        for (i, (dst, n, days)) in routes.iter().take(15).enumerate() {
            if i > 0 {
                buf.push(',');
            }
            let mut item = Obj::new(buf);
            write_str(item.key("dst"), dst);
            item.int("flights", *n).int("days", *days as i64);
            item.end();
        }
        buf.push(']');
        o.end();
        out.insert(by_id[org as usize].clone(), Some(s.into()));
    }
    // airports every leg of which is a circuit: no departures observed
    let codes: Vec<String> = c
        .prepare("SELECT DISTINCT org FROM legs WHERE org IS NOT NULL")?
        .query_map([], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?;
    for code in codes {
        out.entry(code).or_insert(None);
    }
    Ok(Some(out))
}

impl LegBook {
    pub fn new(path: &str) -> Arc<LegBook> {
        Arc::new(LegBook {
            path: path.to_string(),
            inner: Mutex::new(Inner {
                loaded_mtime: None,
                next_check: None,
                through: None,
                window_days: None,
                conn: None,
                airports: HashMap::new(),
                warming: false,
            }),
        })
    }

    /// Re-check the file (at most every five minutes); true when a new
    /// file was loaded.
    fn refresh(&self, g: &mut Inner) -> bool {
        let now = Instant::now();
        if g.next_check.is_some_and(|t| now < t) && g.loaded_mtime.is_some() {
            return false;
        }
        g.next_check = Some(now + RELOAD);
        let Ok(mtime) = std::fs::metadata(&self.path).and_then(|m| m.modified()) else {
            *g = Inner { next_check: g.next_check, ..Inner::empty() };
            return false;
        };
        if g.loaded_mtime == Some(mtime) {
            return false;
        }
        // a file that will not open keeps whatever worked last
        let Ok(conn) = open(&self.path) else { return false };
        g.through = conn.query_row("SELECT MAX(date) FROM legs", [], |r| r.get::<_, Option<String>>(0)).ok().flatten();
        g.window_days = conn
            .query_row("SELECT value FROM meta WHERE key = 'window_days'", [], |r| r.get::<_, Option<String>>(0))
            .ok()
            .flatten()
            .and_then(|v| v.trim().parse().ok());
        g.conn = Some(conn);
        g.loaded_mtime = Some(mtime);
        g.airports.clear();
        true
    }

    pub fn available(self: &Arc<Self>) -> bool {
        let loaded = {
            let mut g = self.inner.lock().unwrap();
            let fresh = self.refresh(&mut g);
            if fresh && !g.warming {
                g.warming = true;
                Some(g.loaded_mtime)
            } else {
                None
            }
        };
        if let Some(mtime) = loaded {
            let me = self.clone();
            std::thread::spawn(move || me.warm(mtime));
        }
        self.inner.lock().unwrap().conn.is_some()
    }

    /// ISO date of the newest leg, None while the archive is dark.
    pub fn archive_through(self: &Arc<Self>) -> Option<String> {
        self.available();
        self.inner.lock().unwrap().through.clone()
    }

    pub fn window_days(self: &Arc<Self>) -> Option<i64> {
        self.available();
        self.inner.lock().unwrap().window_days
    }

    /// `observed` for an airport (IATA), computed once per file. Blocking.
    pub fn airport(self: &Arc<Self>, code: &str) -> Option<Arc<str>> {
        let code = code.trim().to_uppercase();
        if !self.available() {
            return None;
        }
        let mtime = {
            let g = self.inner.lock().unwrap();
            if let Some(hit) = g.airports.get(&code) {
                return hit.clone();
            }
            g.loaded_mtime
        };
        let computed = {
            let c = open(&self.path).ok()?;
            airport_json(&c, &code).ok()?
        };
        let mut g = self.inner.lock().unwrap();
        if g.loaded_mtime == mtime {
            g.airports.insert(code, computed.clone());
        }
        computed
    }

    /// Run `f` on the loaded file's connection (queries are short index
    /// lookups; the lock keeps one connection for all of them). None
    /// while the archive is dark.
    pub fn with_conn<T>(self: &Arc<Self>, f: impl FnOnce(&Connection) -> rusqlite::Result<T>) -> Option<T> {
        if !self.available() {
            return None;
        }
        let g = self.inner.lock().unwrap();
        f(g.conn.as_ref()?).ok()
    }

    /// Compute every airport's totals for the file loaded at `mtime`, on
    /// a connection of its own, so requests find them ready.
    fn warm(&self, mtime: Option<SystemTime>) {
        let started = Instant::now();
        let done = (|| -> rusqlite::Result<usize> {
            let c = open(&self.path)?;
            let current = || self.inner.lock().unwrap().loaded_mtime == mtime;
            // a newer file arrived: its own pass takes over
            let Some(all) = all_airports(&c, &current)? else { return Ok(0) };
            let mut g = self.inner.lock().unwrap();
            if g.loaded_mtime != mtime {
                return Ok(0);
            }
            let mut n = 0;
            for (code, v) in all {
                // an airport a visitor already asked for keeps its answer
                if let std::collections::hash_map::Entry::Vacant(e) = g.airports.entry(code) {
                    e.insert(v);
                    n += 1;
                }
            }
            Ok(n)
        })();
        match done {
            Ok(n) => eprintln!("legs: {n} airports warmed in {:.0} s", started.elapsed().as_secs_f64()),
            Err(e) => eprintln!("legs: warming stopped: {e}"),
        }
        self.inner.lock().unwrap().warming = false;
    }
}

/// `airframe_summary`: an airframe's legs in the window, its last leg,
/// where it likely sits, and the route it flies most. Written as the
/// fields the fleet page takes: legs, last_date, last_org, last_dst,
/// where, top_route.
pub struct AirframeSummary {
    pub legs: i64,
    pub last_date: Option<String>,
    pub last_org: Option<String>,
    pub last_dst: Option<String>,
    pub where_: Option<String>,
    pub top_route: Option<(Option<String>, Option<String>, i64)>,
}

pub fn airframe_summary(c: &Connection, hex: &str) -> rusqlite::Result<Option<AirframeSummary>> {
    let hex = hex.trim().to_lowercase();
    let n: i64 = c.prepare_cached("SELECT COUNT(*) FROM legs WHERE hex = ?")?.query_row([&hex], |r| r.get(0))?;
    if n == 0 {
        return Ok(None);
    }
    let last: (Option<String>, Option<String>, Option<String>, Option<rusqlite::types::Value>) = c
        .prepare_cached("SELECT date, org, dst, arr_ts FROM legs WHERE hex = ? ORDER BY date DESC, dep_ts DESC LIMIT 1")?
        .query_row([&hex], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?;
    let top = c
        .prepare_cached(
            "SELECT org, dst, COUNT(*) FROM legs WHERE hex = ? AND org IS NOT NULL AND dst IS NOT NULL \
             AND org <> dst GROUP BY org, dst ORDER BY 3 DESC LIMIT 1",
        )?
        .query_row([&hex], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .ok();
    // Python's `last[2] if last[4] else None`: an arrival that is set and
    // not zero or empty
    let arrived = match &last.3 {
        None | Some(rusqlite::types::Value::Null) => false,
        Some(rusqlite::types::Value::Integer(i)) => *i != 0,
        Some(rusqlite::types::Value::Real(f)) => *f != 0.0,
        Some(rusqlite::types::Value::Text(t)) => !t.is_empty(),
        Some(rusqlite::types::Value::Blob(b)) => !b.is_empty(),
    };
    Ok(Some(AirframeSummary {
        legs: n,
        where_: if arrived { last.2.clone() } else { None },
        last_date: last.0,
        last_org: last.1,
        last_dst: last.2,
        top_route: top,
    }))
}

/// `callsigns`: flight numbers starting with `prefix`, busiest first,
/// over valid legs (a range, not LIKE: it lands on the prefix directly).
/// (callsign, flights, last date).
pub fn callsigns(c: &Connection, prefix: &str, limit: usize) -> rusqlite::Result<Vec<(String, i64, Option<String>)>> {
    let prefix = prefix.trim().to_uppercase();
    if prefix.is_empty() {
        return Ok(vec![]);
    }
    let upper = format!("{prefix}\u{ffff}");
    c.prepare_cached(
        "SELECT callsign, COUNT(*), MAX(date) FROM legs WHERE callsign >= ? AND callsign < ? \
         AND org IS NOT NULL AND dst IS NOT NULL AND org <> dst GROUP BY callsign ORDER BY 2 DESC, 1 LIMIT ?",
    )?
    .query_map((&prefix, &upper, limit as i64), |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
    .collect()
}

impl Inner {
    fn empty() -> Inner {
        Inner {
            loaded_mtime: None,
            next_check: None,
            through: None,
            window_days: None,
            conn: None,
            airports: HashMap::new(),
            warming: false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every airport over LEGS_BENCH, one index walk each as requests
    /// compute them and in the warm pass's one sweep: the same answers.
    #[test]
    fn one_pass_answers_like_the_walks() {
        let Ok(path) = std::env::var("LEGS_BENCH") else { return };
        let c = open(&path).unwrap();
        let codes: Vec<String> = c
            .prepare("SELECT DISTINCT org FROM legs WHERE org IS NOT NULL")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        let t = Instant::now();
        let walks: Vec<_> = codes.iter().map(|code| airport_json(&c, code).unwrap()).collect();
        let t_walks = t.elapsed();
        let t = Instant::now();
        let all = all_airports(&c, &|| true).unwrap().unwrap();
        eprintln!("{} airports: walks {t_walks:?}, one pass {:?}", codes.len(), t.elapsed());
        assert_eq!(all.len(), codes.len());
        for (code, want) in codes.iter().zip(&walks) {
            assert_eq!(&all[code], want, "{code}");
        }
    }
}
