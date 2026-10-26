//! Partner data: the live sky of a peer network that feeds us under its
//! own agreement, not the feeder terms. It may drive the frames and
//! nothing public: no public route, stream, trace, estimate, squawk,
//! station or release reads it. The fleet tier is its only reader,
//! through `fleet_sky`, which lays the partner's aircraft the network
//! did not hear after the network's own.
//!
//! The partner's stream is its own readsb (NETWORKD_PARTNER_LIVE_JSON,
//! that readsb's JSON position port), never merged into the network's
//! aggregator. The test at the end holds the rule in the source: only
//! this module, the fleet tier and the wiring name it.
//!
//! One public door, by agreement: `map`, the map's partner layer. Live
//! positions of the partner's aircraft the network does not hear, for
//! our own site only, credited, never cached, never in the OpenAPI.
//!
//! Internal use, by agreement: `Store`, the partner's positions sampled
//! into one SQLite file of their own (NETWORKD_PARTNER_STORE), apart from
//! everything the network keeps, in no backup and no release, a rolling
//! window long. The fleet tier reads it for the frames' "your sky"
//! history; ending the agreement deletes the file.

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::{Request, State};
use axum::http::header;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpStream;

use crate::http::{client_ip, json, peer_of, query_param, throttle, ApiError, ApiResult};
use crate::live::parse_bbox;
use crate::pyjson::{write_float, write_str};
use crate::sky::{parse_aircraft_full, Entry, LiveSky, Snapshot, SnapshotCell, F_HEX, F_FLIGHT, F_LAT, F_LON, F_ALT, F_TRACK};
use crate::state::{now_s, App};

/// How often the partner's snapshot is rebuilt while its lines flow.
const PUBLISH_S: f64 = 1.0;
/// What the map's partner layer carries of each aircraft: identity and
/// position (hex, flight, t, lat, lon, alt_baro, gs, track, category).
const MAP_FIELDS: [usize; 9] = [F_HEX, F_FLIGHT, 2, F_LAT, F_LON, F_ALT, 7, F_TRACK, 9];
const MAP_MAX: usize = 3000;
const NO_STORE: &str = "no-store";

pub struct Partner {
    pub store: Option<Store>,
    live: Mutex<LiveSky>,
    snapshot: SnapshotCell,
    merged: Mutex<Option<Merged>>,
}

/// The last merge, kept with the two snapshots it was made from.
struct Merged {
    network: Arc<Snapshot>,
    partner: Arc<Snapshot>,
    sky: Arc<Snapshot>,
}

impl Partner {
    pub fn new(max_aircraft: usize, store: Option<Store>) -> Arc<Partner> {
        Arc::new(Partner {
            store,
            live: Mutex::new(LiveSky::new(max_aircraft)),
            snapshot: SnapshotCell::new(),
            merged: Mutex::new(None),
        })
    }

    pub fn set_snapshot(&self, s: Arc<Snapshot>) {
        self.snapshot.set(s);
    }

    /// The network's snapshot, then the partner's aircraft it does not
    /// hold, while the partner's snapshot is fresh. A stale or absent
    /// partner leaves the network's snapshot as it is.
    pub fn fleet_sky(&self, network: Arc<Snapshot>, stale_after_s: i64) -> Arc<Snapshot> {
        let partner = self.snapshot.get();
        if !partner.fresh(stale_after_s) {
            return network;
        }
        let mut merged = self.merged.lock().unwrap();
        if let Some(m) = merged.as_ref() {
            if Arc::ptr_eq(&m.network, &network) && Arc::ptr_eq(&m.partner, &partner) {
                return m.sky.clone();
            }
        }
        let known: HashSet<&str> = network.entries.iter().map(|e| &*e.hex).collect();
        let copy = |e: &Entry| Entry::with_hidden((*e.vals).clone(), e.hidden.clone());
        let mut entries: Vec<Entry> = network.entries.iter().map(copy).collect();
        entries.extend(partner.entries.iter().filter(|e| !known.contains(&*e.hex)).map(copy));
        let sky = Arc::new(Snapshot::new(network.generated_at, entries));
        *merged = Some(Merged { network, partner, sky: sky.clone() });
        sky
    }
}

/// Keep one connection to the partner readsb's JSON port; merge every
/// line into the partner's own state.
pub async fn read_lines(partner: Arc<Partner>, endpoint: String) {
    let mut backoff = 1.0f64;
    loop {
        match tokio::time::timeout(Duration::from_secs(10), TcpStream::connect(&endpoint)).await {
            Ok(Ok(sock)) => {
                {
                    let mut live = partner.live.lock().unwrap();
                    live.connected = true;
                    live.connected_at = now_s();
                }
                backoff = 1.0;
                eprintln!("partner sky connected to {endpoint}");
                let _ = sock.set_nodelay(true);
                let mut reader = BufReader::with_capacity(1 << 16, sock);
                let mut line = Vec::with_capacity(1024);
                loop {
                    line.clear();
                    match reader.read_until(b'\n', &mut line).await {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {}
                    }
                    let Ok(text) = std::str::from_utf8(&line) else { continue };
                    let Some((vals, hidden)) = parse_aircraft_full(text.trim_end()) else { continue };
                    partner.live.lock().unwrap().ingest_full(vals, hidden, now_s());
                }
                partner.live.lock().unwrap().connected = false;
            }
            Ok(Err(e)) => eprintln!("partner sky {endpoint}: {e}"),
            Err(_) => eprintln!("partner sky {endpoint}: connect timed out"),
        }
        eprintln!("partner sky reconnecting in {backoff:.0} s");
        tokio::time::sleep(Duration::from_secs_f64(backoff)).await;
        backoff = (backoff * 2.0).min(30.0);
    }
}

/// While the partner's lines flow, rebuild its snapshot on change. When
/// they stop, the snapshot ages out and the fleet tier stops using it.
pub async fn publish(partner: Arc<Partner>) {
    let mut seen_version = 0;
    let mut tick = tokio::time::interval(Duration::from_secs_f64(PUBLISH_S));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tick.tick().await;
        let now = now_s();
        let snap = {
            let mut live = partner.live.lock().unwrap();
            if live.version == seen_version || !live.fresh(now) {
                continue;
            }
            seen_version = live.version;
            Arc::new(live.snapshot(now))
        };
        partner.set_snapshot(snap);
    }
}

// ---- the internal store -------------------------------------------------

const STORE_SCHEMA: &str = "
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS points (
    ts INTEGER NOT NULL,
    cell INTEGER NOT NULL,
    hex TEXT NOT NULL,
    flight TEXT,
    t TEXT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    alt INTEGER,
    gs REAL,
    track REAL
);
CREATE INDEX IF NOT EXISTS ix_points_cell_ts ON points (cell, ts);
CREATE INDEX IF NOT EXISTS ix_points_ts ON points (ts);
";

/// Most aircraft one "near" answer lists.
const NEAR_MAX: usize = 500;

/// The partner's positions, sampled, in their own file.
pub struct Store {
    conn: Mutex<rusqlite::Connection>,
    keep_s: i64,
}

/// One aircraft seen near a place: when, and how close it came.
#[derive(Debug, PartialEq)]
pub struct Seen {
    pub hex: String,
    pub flight: Option<String>,
    pub t: Option<String>,
    pub first: i64,
    pub last: i64,
    pub closest_nm: f64,
}

/// 1° cells, numbered row by row from the south-west.
fn cell(lat: f64, lon: f64) -> i64 {
    let y = ((lat + 90.0).floor() as i64).clamp(0, 179);
    let x = ((lon + 180.0).floor() as i64).rem_euclid(360);
    y * 360 + x
}

fn text(v: &Option<crate::pyjson::Val>) -> Option<String> {
    match v {
        Some(crate::pyjson::Val::Str(s)) if !s.trim().is_empty() => Some(s.trim().to_string()),
        _ => None,
    }
}

impl Store {
    pub fn from_settings(s: &crate::settings::Settings) -> Option<Store> {
        if s.partner_store.is_empty() {
            return None;
        }
        match Store::open(&s.partner_store, s.partner_keep_days) {
            Ok(st) => Some(st),
            Err(e) => {
                eprintln!("partner store {}: {e}", s.partner_store);
                None
            }
        }
    }

    pub fn open(path: &str, keep_days: i64) -> rusqlite::Result<Store> {
        let conn = rusqlite::Connection::open(path)?;
        conn.execute_batch(STORE_SCHEMA)?;
        Ok(Store { conn: Mutex::new(conn), keep_s: keep_days.max(1) * 86_400 })
    }

    /// Every positioned aircraft of `snap`, stamped `at`.
    pub fn record(&self, snap: &Snapshot, at: i64) -> rusqlite::Result<usize> {
        use crate::pyjson::Val;
        let mut c = self.conn.lock().unwrap();
        let tx = c.transaction()?;
        let mut n = 0;
        {
            let mut ins = tx.prepare_cached(
                "INSERT INTO points (ts, cell, hex, flight, t, lat, lon, alt, gs, track) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
            )?;
            for e in &snap.entries {
                let (Some(lat), Some(lon)) = (e.lat, e.lon) else { continue };
                let alt = match &e.vals[F_ALT] {
                    Some(Val::Str(g)) if &**g == "ground" => Some(0),
                    Some(v) => v.as_f64().map(|x| x as i64),
                    None => None,
                };
                let num = |i: usize| e.vals[i].as_ref().and_then(Val::as_f64);
                ins.execute(rusqlite::params![
                    at,
                    cell(lat, lon),
                    &*e.hex,
                    text(&e.vals[F_FLIGHT]),
                    text(&e.vals[2]),
                    lat,
                    lon,
                    alt,
                    num(7),
                    num(F_TRACK)
                ])?;
                n += 1;
            }
        }
        tx.commit()?;
        Ok(n)
    }

    /// Drop what is older than the window.
    pub fn prune(&self, now: i64) -> rusqlite::Result<usize> {
        self.conn.lock().unwrap().execute("DELETE FROM points WHERE ts < ?1", [now - self.keep_s])
    }

    /// Aircraft seen within `radius_nm` of a place since `since`, the
    /// closest first.
    pub fn near(&self, lat: f64, lon: f64, radius_nm: f64, since: i64) -> rusqlite::Result<Vec<Seen>> {
        let dlat = radius_nm / 60.0;
        let dlon = radius_nm / (60.0 * lat.to_radians().cos().abs().max(0.05));
        let mut cells = vec![];
        let (y0, y1) = ((lat - dlat).max(-89.999), (lat + dlat).min(89.999));
        let mut y = y0.floor();
        while y <= y1.floor() {
            let mut x = (lon - dlon).floor();
            while x <= (lon + dlon).floor() {
                cells.push(cell(y, x));
                x += 1.0;
            }
            y += 1.0;
        }
        cells.sort_unstable();
        cells.dedup();
        let c = self.conn.lock().unwrap();
        let mut q = c.prepare_cached(
            "SELECT hex, flight, t, ts, lat, lon FROM points WHERE cell = ?1 AND ts >= ?2 ORDER BY ts",
        )?;
        let mut by_hex: std::collections::HashMap<String, Seen> = std::collections::HashMap::new();
        for cl in cells {
            let rows = q.query_map(rusqlite::params![cl, since], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, Option<String>>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, f64>(4)?,
                    r.get::<_, f64>(5)?,
                ))
            })?;
            for row in rows {
                let (hex, flight, t, ts, plat, plon) = row?;
                let d = crate::live::distance_nm(lat, lon, plat, plon);
                if d > radius_nm {
                    continue;
                }
                let s = by_hex.entry(hex.clone()).or_insert(Seen {
                    hex,
                    flight: None,
                    t: None,
                    first: ts,
                    last: ts,
                    closest_nm: d,
                });
                s.first = s.first.min(ts);
                if ts >= s.last {
                    s.last = ts;
                    s.flight = flight.or(s.flight.take());
                }
                s.t = s.t.take().or(t);
                s.closest_nm = s.closest_nm.min(d);
            }
        }
        let mut out: Vec<Seen> = by_hex.into_values().collect();
        out.sort_by(|a, b| a.closest_nm.total_cmp(&b.closest_nm));
        out.truncate(NEAR_MAX);
        Ok(out)
    }
}

/// Sample the partner's fresh snapshot into the store every `every_s`,
/// and prune the window once an hour.
pub async fn keep(partner: Arc<Partner>, every_s: f64) {
    let mut tick = tokio::time::interval(Duration::from_secs_f64(every_s.max(10.0)));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut pruned = 0i64;
    loop {
        tick.tick().await;
        let snap = partner.snapshot.get();
        if !snap.fresh(60) {
            continue;
        }
        let p = partner.clone();
        let now = now_s() as i64;
        let prune = now - pruned >= 3600;
        if prune {
            pruned = now;
        }
        let done = tokio::task::spawn_blocking(move || -> rusqlite::Result<()> {
            let Some(st) = &p.store else { return Ok(()) };
            st.record(&snap, now)?;
            if prune {
                st.prune(now)?;
            }
            Ok(())
        })
        .await;
        if let Ok(Err(e)) = done {
            eprintln!("partner store: {e}");
        }
    }
}

/// The request comes from one of the site's own pages: its Origin, or
/// failing that its Referer, is a listed origin.
fn from_site(origins: &[String], headers: &axum::http::HeaderMap) -> bool {
    let get = |h| headers.get(h).and_then(|v| v.to_str().ok());
    if let Some(o) = get(header::ORIGIN) {
        return origins.iter().any(|x| x == o.trim_end_matches('/'));
    }
    get(header::REFERER).is_some_and(|r| origins.iter().any(|x| r == x || r.starts_with(&format!("{x}/"))))
}

/// GET /v1/map/partner?bbox=: the partner's live aircraft the network
/// does not hear, for our map. Positions only: no history, no search, no
/// fields beyond MAP_FIELDS. Answered only to the site's own pages,
/// throttled, `no-store`. A browser page elsewhere gets 403; a scraper
/// can forge an Origin, which is why the agreement promises "never
/// offered", not "impossible to copy".
pub async fn map(State(app): State<Arc<App>>, req: Request) -> ApiResult {
    let s = &app.settings;
    let partner = match &app.partner {
        Some(p) if !s.partner_name.is_empty() => p,
        _ => return Err(ApiError::new(404, "not_found", "Not Found")),
    };
    if !from_site(&s.partner_map_origins, req.headers()) {
        return Err(ApiError::new(403, "forbidden", "this layer belongs to the FlightPortrait map"));
    }
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    throttle(&app, &ip, "partner_map", s.partner_map_rate_limit)?;
    let bbox = match query_param(req.uri().query(), "bbox").as_deref() {
        Some(b) if b.chars().count() > 80 => {
            return Err(ApiError::new(422, "invalid_request", "query.bbox: String should have at most 80 characters"))
        }
        Some(b) if !b.is_empty() => Some(parse_bbox(b)?),
        _ => None,
    };
    let theirs = partner.snapshot.get();
    let ours = app.snapshot();
    let mut body = String::with_capacity(4096);
    body.push_str("{\"credit\":{\"name\":");
    write_str(&mut body, &s.partner_name);
    body.push_str(",\"url\":");
    write_str(&mut body, &s.partner_url);
    body.push_str("},\"now\":");
    write_float(&mut body, theirs.generated_at);
    body.push_str(",\"aircraft\":[");
    if theirs.fresh(s.stale_after_s) {
        let known: HashSet<&str> = ours.entries.iter().map(|e| &*e.hex).collect();
        let mut n = 0;
        for e in &theirs.entries {
            if !e.has_pos || known.contains(&*e.hex) || bbox.as_ref().is_some_and(|b| !e.in_bbox(b)) {
                continue;
            }
            if n > 0 {
                body.push(',');
            }
            body.push('{');
            let mut first = true;
            for f in MAP_FIELDS {
                if let Some(kv) = e.field(f) {
                    if !first {
                        body.push(',');
                    }
                    body.push_str(kv);
                    first = false;
                }
            }
            body.push('}');
            n += 1;
            if n >= MAP_MAX {
                break;
            }
        }
    }
    body.push_str("]}");
    Ok(json(body, NO_STORE))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snap(at: f64, rows: &[&str]) -> Arc<Snapshot> {
        let entries = rows
            .iter()
            .map(|r| {
                let (v, h) = parse_aircraft_full(r).unwrap();
                Entry::with_hidden(v, h)
            })
            .collect();
        Arc::new(Snapshot::new(at, entries))
    }

    #[test]
    fn the_store_keeps_positions_answers_near_and_forgets_the_old() {
        let path = std::env::temp_dir().join(format!("networkd-partner-store-{}.sqlite", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let st = Store::open(path.to_str().unwrap(), 14).unwrap();
        let t0 = 1_800_000_000;
        let a = snap(now_s(), &[
            r#"{"hex":"beef01","flight":"PRTNR1 ","t":"A20N","lat":1.30,"lon":103.80,"alt_baro":9000}"#,
            r#"{"hex":"beef02","flight":"FAR1","lat":10.0,"lon":110.0}"#,
            r#"{"hex":"beef03","flight":"NOPOS"}"#,
        ]);
        assert_eq!(st.record(&a, t0).unwrap(), 2);
        let b = snap(now_s(), &[r#"{"hex":"beef01","flight":"PRTNR1","lat":1.36,"lon":103.82,"alt_baro":"ground"}"#]);
        st.record(&b, t0 + 120).unwrap();
        let near = st.near(1.3521, 103.8198, 10.0, t0 - 1).unwrap();
        assert_eq!(near.len(), 1);
        let s = &near[0];
        assert_eq!((s.hex.as_str(), s.flight.as_deref(), s.t.as_deref()), ("beef01", Some("PRTNR1"), Some("A20N")));
        assert_eq!((s.first, s.last), (t0, t0 + 120));
        assert!(s.closest_nm < 1.0, "{}", s.closest_nm);
        // since cuts the old sighting
        assert_eq!(st.near(1.3521, 103.8198, 10.0, t0 + 60).unwrap()[0].first, t0 + 120);
        // a box across the antimeridian finds nothing it should not
        assert!(st.near(0.0, 179.99, 30.0, 0).unwrap().is_empty());
        // the window: 14 days later the first sample is gone
        st.prune(t0 + 14 * 86_400 + 60).unwrap();
        assert_eq!(st.near(1.3521, 103.8198, 10.0, 0).unwrap()[0].first, t0 + 120);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn the_partner_adds_only_what_the_network_did_not_hear() {
        let p = Partner::new(1000, None);
        let network = snap(now_s(), &[r#"{"hex":"aaaaaa","lat":1.0,"lon":103.0}"#]);
        // nothing from the partner yet: the network's own snapshot
        assert!(Arc::ptr_eq(&p.fleet_sky(network.clone(), 30), &network));
        p.set_snapshot(snap(
            now_s(),
            &[r#"{"hex":"aaaaaa","lat":9.0,"lon":9.0}"#, r#"{"hex":"bbbbbb","lat":1.1,"lon":103.1}"#],
        ));
        let sky = p.fleet_sky(network.clone(), 30);
        let hexes: Vec<&str> = sky.entries.iter().map(|e| &*e.hex).collect();
        assert_eq!(hexes, ["aaaaaa", "bbbbbb"]);
        // the network's own report of an aircraft wins over the partner's
        assert_eq!(sky.entries[0].lat, Some(1.0));
        // the same two snapshots give the same merge
        assert!(Arc::ptr_eq(&sky, &p.fleet_sky(network.clone(), 30)));
        // a stale partner drops out
        p.set_snapshot(snap(now_s() - 120.0, &[r#"{"hex":"bbbbbb","lat":1.1,"lon":103.1}"#]));
        assert!(Arc::ptr_eq(&p.fleet_sky(network.clone(), 30), &network));
    }

    /// The rule in the source: partner data is named only where it is
    /// read (here and the fleet tier) and wired (state, main, settings).
    /// A new reader anywhere else fails this test, on purpose.
    #[test]
    fn only_the_fleet_tier_reads_partner_data() {
        let allowed = ["partner.rs", "fleet.rs", "state.rs", "main.rs", "settings.rs"];
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut stack = vec![dir];
        let mut offenders = vec![];
        while let Some(d) = stack.pop() {
            for f in std::fs::read_dir(&d).unwrap() {
                let path = f.unwrap().path();
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                let name = path.file_name().unwrap().to_string_lossy().to_string();
                if !name.ends_with(".rs") || allowed.contains(&name.as_str()) {
                    continue;
                }
                let text = std::fs::read_to_string(&path).unwrap().to_lowercase();
                if text.contains("partner") {
                    offenders.push(path.display().to_string());
                }
            }
        }
        assert!(offenders.is_empty(), "partner data named outside the fleet tier: {offenders:?}");
    }
}
