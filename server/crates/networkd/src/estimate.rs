//! Estimated positions for aircraft that left coverage (the Python
//! service's estimate.py, estimate_store.py and the /v1/estimated route).
//!
//! When a cruising aircraft stops being heard, the map keeps drawing it
//! where it most likely is: flown on from its last observed position
//! toward the destination its callsign's route names, at its last ground
//! speed, holding its track ten minutes and then turning toward the
//! destination. Served apart from observations, never archived. The
//! gates and the method were chosen on the Python backtest
//! (network/docs/estimates.md); the arithmetic here follows it
//! operation for operation, so both draw the same point.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::{Request, State};
use axum::response::{IntoResponse, Response};
use indexmap::IndexMap;
use serde_json::Value;

use crate::http::{client_ip, json, peer_of, throttle};
use crate::pg::Lazy;
use crate::pyjson::{round_to, write_dumps, write_float, Obj, Val};
use crate::routebook::RouteBook;
use crate::sky::{Vals, F_ALT, F_FLIGHT, F_HEX, F_LAT, F_LON, F_SEEN_POS, F_TRACK};
use crate::state::{now_s, App};

const EARTH_KM: f64 = 6371.0088;
const KT_TO_KMS: f64 = 1.852 / 3600.0;

const MIN_ALT_FT: f64 = 18000.0;
const MIN_GS_KT: f64 = 250.0;
const MIN_REMAINING_KM: f64 = 250.0;
const MAX_OFF_BEARING_DEG: f64 = 100.0;
const MAX_AGE_S: f64 = 8.0 * 3600.0;
const ETA_SLACK: f64 = 1.1;
const STOP_BEFORE_KM: f64 = 150.0;
const TURN_DEG_PER_MIN: f64 = 2.0;
const HOLD_S: f64 = 600.0;
const STEP_S: f64 = 30.0;
const LOST_AFTER_S: f64 = 90.0;
const SHOW_MAX_S: f64 = 15.0 * 60.0;

/// A callsign's route is looked up once an hour at most.
const ROUTE_TTL_S: f64 = 3600.0;
const ROUTE_CACHE_MAX: usize = 20000;
/// The inferred schedule names a route when one leg is flown this share
/// of the time.
const SCHEDULE_DOMINANT: f64 = 0.7;
/// The book's memory is saved this often (and on the way out).
const SAVE_S: u64 = 60;
const STATE_KEY: &str = "estimate_book";

const F_T: usize = 2;
const F_R: usize = 3;
const F_GS: usize = 7;
const F_CATEGORY: usize = 9;

// ---- geometry, as Python computes it ---------------------------------------

/// Python's float `%`: the result takes the divisor's sign.
fn py_mod(a: f64, b: f64) -> f64 {
    let m = a % b;
    if m != 0.0 {
        if (b < 0.0) != (m < 0.0) {
            m + b
        } else {
            m
        }
    } else {
        0.0f64.copysign(b)
    }
}

fn haversine_km(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let (p1, p2) = (lat1.to_radians(), lat2.to_radians());
    let (dp, dl) = (p2 - p1, (lon2 - lon1).to_radians());
    let s1 = (dp / 2.0).sin();
    let s2 = (dl / 2.0).sin();
    let a = s1 * s1 + p1.cos() * p2.cos() * (s2 * s2);
    2.0 * EARTH_KM * a.sqrt().min(1.0).asin()
}

fn bearing_deg(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let (p1, p2) = (lat1.to_radians(), lat2.to_radians());
    let dl = (lon2 - lon1).to_radians();
    let y = dl.sin() * p2.cos();
    let x = p1.cos() * p2.sin() - p1.sin() * p2.cos() * dl.cos();
    py_mod(y.atan2(x).to_degrees() + 360.0, 360.0)
}

fn forward(lat: f64, lon: f64, heading_deg: f64, dist_km: f64) -> (f64, f64) {
    let d = dist_km / EARTH_KM;
    let h = heading_deg.to_radians();
    let (p1, l1) = (lat.to_radians(), lon.to_radians());
    let p2 = (p1.sin() * d.cos() + p1.cos() * d.sin() * h.cos()).asin();
    let l2 = l1 + (h.sin() * d.sin() * p1.cos()).atan2(d.cos() - p1.sin() * p2.sin());
    (p2.to_degrees(), py_mod(l2.to_degrees() + 540.0, 360.0) - 180.0)
}

/// Signed smallest difference b - a, in degrees (-180, 180].
fn angle_diff(a: f64, b: f64) -> f64 {
    let d = py_mod(b - a + 180.0, 360.0) - 180.0;
    if d == -180.0 {
        180.0
    } else {
        d
    }
}

/// Python's min/max: the first argument wins a tie.
fn py_min(a: f64, b: f64) -> f64 {
    if b < a {
        b
    } else {
        a
    }
}

fn py_max(a: f64, b: f64) -> f64 {
    if b > a {
        b
    } else {
        a
    }
}

/// A route as an airport chain (a code that is not a string is None).
type Chain = Vec<Option<String>>;

#[derive(Clone, Debug, PartialEq)]
struct Dest {
    code: String,
    lat: f64,
    lon: f64,
    dist: f64,
}

/// The first stop of the route, in order, that is ahead of the aircraft
/// and far enough away; stops behind it are legs already flown.
fn pick_destination(
    lat: f64,
    lon: f64,
    track: f64,
    chain: &[Option<String>],
    airports: &HashMap<String, (f64, f64)>,
) -> Option<Dest> {
    for code in chain.iter().skip(1).flatten() {
        let Some(&(alat, alon)) = airports.get(code) else { continue };
        let dist = haversine_km(lat, lon, alat, alon);
        let off = angle_diff(track, bearing_deg(lat, lon, alat, alon)).abs();
        if off <= MAX_OFF_BEARING_DEG && dist >= MIN_REMAINING_KM {
            return Some(Dest { code: code.clone(), lat: alat, lon: alon, dist });
        }
    }
    None
}

// ---- the book ---------------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
enum Found {
    Unknown,
    Nowhere,
    At(Dest),
}

/// The last cruising observation of one aircraft. The raw values are
/// kept to be written back as they came.
#[derive(Clone, Debug)]
struct Obs {
    lat: Val,
    lon: Val,
    alt: Val,
    gs: Val,
    track: Val,
    flight: String,
    t: Val,
    r: Val,
    category: Val,
    at: f64,
    dest: Found,
}

fn num(v: &Val) -> Option<f64> {
    match v {
        Val::Int(_) | Val::UInt(_) | Val::Float(_) => v.as_f64(),
        _ => None,
    }
}

impl Obs {
    fn lat(&self) -> f64 {
        num(&self.lat).unwrap_or(f64::NAN)
    }
    fn lon(&self) -> f64 {
        num(&self.lon).unwrap_or(f64::NAN)
    }
    fn gs(&self) -> f64 {
        num(&self.gs).unwrap_or(0.0)
    }
    fn track(&self) -> f64 {
        num(&self.track).unwrap_or(f64::NAN)
    }
}

/// Cruising, fast, with a heading and a position.
fn eligible(lat: &Val, lon: &Val, alt: &Val, gs: &Val, track: &Val) -> bool {
    num(alt).is_some_and(|a| a >= MIN_ALT_FT)
        && num(gs).unwrap_or(0.0) >= MIN_GS_KT
        && !track.is_null()
        && !lat.is_null()
        && !lon.is_null()
}

/// Where the aircraft is dt_s after obs, converging on dest: its last
/// track for HOLD_S, then a turn toward dest at TURN_DEG_PER_MIN, one
/// STEP_S at a time. (lat, lon, heading, remaining km, heading is still
/// the observed track).
fn project(obs: &Obs, dest: (f64, f64), dt_s: f64) -> (f64, f64, f64, f64, bool) {
    let (mut lat, mut lon, mut hdg) = (obs.lat(), obs.lon(), obs.track());
    let mut untouched = true;
    let kms = obs.gs() * KT_TO_KMS;
    let (mut t, max_turn) = (0.0, TURN_DEG_PER_MIN * STEP_S / 60.0);
    while t < dt_s {
        let step = py_min(STEP_S, dt_s - t);
        let rem = haversine_km(lat, lon, dest.0, dest.1);
        if rem <= kms * step {
            return (dest.0, dest.1, hdg, 0.0, untouched);
        }
        if t >= HOLD_S {
            let want = bearing_deg(lat, lon, dest.0, dest.1);
            let turn = angle_diff(hdg, want);
            hdg = py_mod(hdg + py_max(-max_turn, py_min(max_turn, turn)), 360.0);
            untouched = false;
        }
        (lat, lon) = forward(lat, lon, hdg, kms * step);
        t += step;
    }
    (lat, lon, hdg, haversine_km(lat, lon, dest.0, dest.1), untouched)
}

/// How long an estimate may run: until STOP_BEFORE_KM out, with slack.
fn horizon_s(obs: &Obs, remaining_km: f64) -> f64 {
    let kms = py_max(obs.gs(), MIN_GS_KT) * KT_TO_KMS;
    let flying = py_max(0.0, remaining_km - STOP_BEFORE_KM) / kms;
    py_min(MAX_AGE_S, flying * ETA_SLACK)
}

#[derive(Default)]
pub struct Book {
    /// hex -> last cruising observation, in the order first kept
    last: IndexMap<String, Obs>,
    live: std::collections::HashSet<String>,
}

fn val(v: &Option<Val>) -> Val {
    v.clone().unwrap_or(Val::Null)
}

impl Book {
    pub fn observe<'a>(&mut self, aircraft: impl Iterator<Item = &'a Vals>, now: f64) {
        let mut live = std::collections::HashSet::new();
        for a in aircraft {
            let hex = match &a[F_HEX] {
                Some(Val::Str(s)) if !s.is_empty() => s.to_string(),
                _ => continue,
            };
            let (lat, lon) = (val(&a[F_LAT]), val(&a[F_LON]));
            if lat.is_null() {
                continue;
            }
            live.insert(hex.clone());
            let (alt, gs, track) = (val(&a[F_ALT]), val(&a[F_GS]), val(&a[F_TRACK]));
            if !eligible(&lat, &lon, &alt, &gs, &track) {
                self.last.shift_remove(&hex);
                continue;
            }
            let flight = match &a[F_FLIGHT] {
                Some(Val::Str(s)) => s.trim().to_string(),
                _ => String::new(),
            };
            let seen_pos = a[F_SEEN_POS].as_ref().and_then(|v| v.as_f64()).unwrap_or(0.0);
            let obs = Obs {
                lat,
                lon,
                alt,
                gs,
                track,
                flight,
                t: val(&a[F_T]),
                r: val(&a[F_R]),
                category: val(&a[F_CATEGORY]),
                at: now - seen_pos,
                dest: Found::Unknown,
            };
            self.last.insert(hex, obs);
        }
        self.live = live;
    }

    /// The flights whose route the next estimates() will ask for.
    fn wanted(&self, now: f64) -> Vec<String> {
        let mut out = vec![];
        for (hex, obs) in &self.last {
            let dt = now - obs.at;
            if self.live.contains(hex) || dt > MAX_AGE_S || !(LOST_AFTER_S..=SHOW_MAX_S).contains(&dt) {
                continue;
            }
            if !obs.flight.is_empty() && obs.dest == Found::Unknown {
                out.push(obs.flight.clone());
            }
        }
        out
    }

    /// The aircraft to draw now, as JSON objects; drops the ones past
    /// their horizon or near their destination. `route` answers a
    /// callsign (None: not resolved yet, tried again next time).
    fn estimates(
        &mut self,
        now: f64,
        route: &dyn Fn(&str) -> Option<Option<Chain>>,
        airports: &HashMap<String, (f64, f64)>,
    ) -> Vec<String> {
        let mut out = vec![];
        let keys: Vec<String> = self.last.keys().cloned().collect();
        for hex in keys {
            if self.live.contains(&hex) {
                continue;
            }
            let obs = self.last.get_mut(&hex).unwrap();
            let dt = now - obs.at;
            if dt > MAX_AGE_S {
                self.last.shift_remove(&hex);
                continue;
            }
            if dt < LOST_AFTER_S || dt > SHOW_MAX_S || obs.flight.is_empty() {
                continue;
            }
            if obs.dest == Found::Unknown {
                let Some(chain) = route(&obs.flight) else { continue };
                obs.dest = match pick_destination(obs.lat(), obs.lon(), obs.track(), &chain.unwrap_or_default(), airports)
                {
                    Some(d) => Found::At(d),
                    None => Found::Nowhere,
                };
            }
            let Found::At(dest) = obs.dest.clone() else { continue };
            if dt > horizon_s(obs, dest.dist) {
                self.last.shift_remove(&hex);
                continue;
            }
            let (lat, lon, hdg, rem, untouched) = project(obs, (dest.lat, dest.lon), dt);
            if rem < STOP_BEFORE_KM {
                self.last.shift_remove(&hex);
                continue;
            }
            let mut s = String::with_capacity(360);
            let mut o = Obj::new(&mut s);
            o.str("hex", &hex).str("flight", &obs.flight);
            obs.t.write(o.key("t"));
            obs.r.write(o.key("r"));
            obs.category.write(o.key("category"));
            o.f64("lat", round_to(lat, 4)).f64("lon", round_to(lon, 4));
            match (&obs.track, untouched) {
                // round(int, 1) stays an int in Python
                (Val::Int(_) | Val::UInt(_), true) => obs.track.write(o.key("track")),
                _ => write_float(o.key("track"), round_to(hdg, 1)),
            }
            obs.alt.write(o.key("alt_baro"));
            obs.gs.write(o.key("gs"));
            o.raw("estimated", "true");
            {
                let ls = o.key("last_seen");
                ls.push_str("{\"at\":");
                write_float(ls, round_to(obs.at, 1));
                ls.push_str(",\"lat\":");
                obs.lat.write(ls);
                ls.push_str(",\"lon\":");
                obs.lon.write(ls);
                ls.push('}');
            }
            o.str("destination", &dest.code);
            o.int("eta", (now + rem / (obs.gs() * KT_TO_KMS)).round_ties_even() as i64);
            o.end();
            out.push(s);
        }
        out
    }

    /// The book's memory as `json.dumps` writes the Python book's dump.
    fn dump(&self) -> String {
        let mut items = vec![];
        for (hex, obs) in &self.last {
            let mut m = serde_json::Map::new();
            let v = |x: &Val| -> Value {
                let mut s = String::new();
                x.write(&mut s);
                serde_json::from_str(&s).unwrap_or(Value::Null)
            };
            m.insert("lat".into(), v(&obs.lat));
            m.insert("lon".into(), v(&obs.lon));
            m.insert("alt".into(), v(&obs.alt));
            m.insert("gs".into(), v(&obs.gs));
            m.insert("track".into(), v(&obs.track));
            m.insert("hex".into(), Value::String(hex.clone()));
            m.insert("flight".into(), Value::String(obs.flight.clone()));
            m.insert("t".into(), v(&obs.t));
            m.insert("r".into(), v(&obs.r));
            m.insert("category".into(), v(&obs.category));
            m.insert("at".into(), serde_json::Number::from_f64(obs.at).map_or(Value::Null, Value::Number));
            items.push(Value::Object(m));
        }
        let mut s = String::new();
        write_dumps(&mut s, &Value::Array(items));
        s
    }

    /// Take back a dump: observations within MAX_AGE_S, never over one
    /// the live sky already refreshed.
    fn restore(&mut self, items: &Value, now: f64) -> usize {
        let mut n = 0;
        for item in items.as_array().into_iter().flatten() {
            let Some(hex) = item.get("hex").and_then(|h| h.as_str()).filter(|h| !h.is_empty()) else { continue };
            if self.last.contains_key(hex) {
                continue;
            }
            let Some(at) = item.get("at").and_then(|a| a.as_f64()) else { continue };
            let g = |k: &str| item.get(k).map(Val::from_json).unwrap_or(Val::Null);
            let (lat, lon, alt, gs, track) = (g("lat"), g("lon"), g("alt"), g("gs"), g("track"));
            if now - at > MAX_AGE_S || !eligible(&lat, &lon, &alt, &gs, &track) {
                continue;
            }
            let flight = item.get("flight").and_then(|f| f.as_str()).unwrap_or("").to_string();
            self.last.insert(
                hex.to_string(),
                Obs { lat, lon, alt, gs, track, flight, t: g("t"), r: g("r"), category: g("category"), at, dest: Found::Unknown },
            );
            n += 1;
        }
        n
    }
}

// ---- routes -------------------------------------------------------------------

/// The inferred schedule's legs for one callsign, [(org, dst, n)], as an
/// airport chain: one leg is its route; legs linked end to end are one
/// chain; else the leg flown SCHEDULE_DOMINANT of the time, or nothing.
fn schedule_chain(legs: &[(Option<String>, Option<String>, Option<i64>)]) -> Option<Chain> {
    let legs: Vec<(String, String, i64)> = legs
        .iter()
        .filter_map(|(o, d, n)| match (o, d) {
            (Some(o), Some(d)) if !o.is_empty() && !d.is_empty() && o != d => Some((o.clone(), d.clone(), n.unwrap_or(0))),
            _ => None,
        })
        .collect();
    if legs.is_empty() {
        return None;
    }
    if legs.len() == 1 {
        return Some(vec![Some(legs[0].0.clone()), Some(legs[0].1.clone())]);
    }
    let mut nxt: Option<IndexMap<&str, &str>> = Some(IndexMap::new());
    let mut ins = std::collections::HashSet::new();
    for (o, d, _) in &legs {
        let map = nxt.as_mut().unwrap();
        if map.contains_key(o.as_str()) || ins.contains(d.as_str()) {
            nxt = None; // a fork: not one line
            break;
        }
        map.insert(o, d);
        ins.insert(d.as_str());
    }
    if let Some(nxt) = nxt {
        let starts: Vec<&str> = nxt.keys().copied().filter(|o| !ins.contains(o)).collect();
        if starts.len() == 1 {
            let mut chain = vec![starts[0]];
            while let Some(next) = nxt.get(chain[chain.len() - 1]) {
                if chain.len() > legs.len() {
                    break;
                }
                chain.push(next);
            }
            if chain.len() == legs.len() + 1 {
                return Some(chain.into_iter().map(|c| Some(c.to_string())).collect());
            }
        }
    }
    let total: i64 = legs.iter().map(|l| l.2).sum();
    let mut best = &legs[0];
    for l in &legs[1..] {
        if l.2 > best.2 {
            best = l;
        }
    }
    if total != 0 && best.2 as f64 / total as f64 >= SCHEDULE_DOMINANT {
        return Some(vec![Some(best.0.clone()), Some(best.1.clone())]);
    }
    None
}

fn chain_of(v: &Value) -> Option<Chain> {
    v.as_array().map(|a| a.iter().map(|x| x.as_str().map(str::to_string)).collect())
}

pub struct Estimator {
    book: Mutex<Book>,
    routes: Arc<RouteBook>,
    db: Option<Lazy>,
    route_cache: Mutex<HashMap<String, (f64, Option<Chain>)>>,
    /// the last night's accuracy as JSON, and when it was looked up
    accuracy: tokio::sync::Mutex<(f64, Option<String>)>,
}

impl Estimator {
    pub fn new(routes: Arc<RouteBook>, database_url: &str) -> Arc<Estimator> {
        Arc::new(Estimator {
            book: Mutex::new(Book::default()),
            routes,
            db: (!database_url.is_empty()).then(|| Lazy::new(database_url)),
            route_cache: Mutex::new(HashMap::new()),
            accuracy: tokio::sync::Mutex::new((0.0, None)),
        })
    }

    pub fn observe<'a>(&self, aircraft: impl Iterator<Item = &'a Vals>, now: f64) {
        self.book.lock().unwrap().observe(aircraft, now);
    }

    /// A callsign's route as /v1/routes resolves it: the observed routes
    /// artifact, else the community catalog in force today, else the
    /// inferred schedule. A failed lookup is no route (cached too).
    async fn resolve(&self, app: &App, callsign: &str) -> Option<Chain> {
        if let Some(v) = self.routes.get(callsign) {
            return chain_of(&v);
        }
        let catalog: Result<Option<Chain>, ()> = async {
            let Some(db) = &self.db else { return Ok(None) };
            let g = db.get().await.map_err(|_| ())?;
            let today = chrono::Utc::now().date_naive();
            let row = g
                .as_ref()
                .unwrap()
                .query_opt(
                    "SELECT origin, via::text, dest FROM route_catalog WHERE callsign = $1 AND valid_from <= $2 \
                     AND (valid_to IS NULL OR valid_to > $2) ORDER BY valid_from DESC LIMIT 1",
                    &[&callsign, &today],
                )
                .await
                .map_err(|_| ())?;
            Ok(row.map(|r| {
                let via: Option<String> = r.get(1);
                let via: Vec<Option<String>> =
                    via.and_then(|t| serde_json::from_str::<Value>(&t).ok()).and_then(|v| chain_of(&v)).unwrap_or_default();
                let mut chain = vec![r.get::<_, Option<String>>(0)];
                chain.extend(via);
                chain.push(r.get::<_, Option<String>>(2));
                chain
            }))
        }
        .await;
        match catalog {
            Err(()) => None,
            Ok(Some(chain)) => Some(chain),
            Ok(None) => {
                // SQLite reads block: off the async workers
                let (db, cs) = (app.refdb.clone(), callsign.to_string());
                tokio::task::spawn_blocking(move || Estimator::schedule(&db, &cs)).await.ok().flatten()
            }
        }
    }

    fn schedule(refdb: &crate::refdb::RefDb, callsign: &str) -> Option<Chain> {
        if !refdb.has(&["ref_schedule"]) {
            return None;
        }
        let c = refdb.conn().ok()?;
        // Postgres reads these through the (callsign, org, dst) key
        let legs: Vec<(Option<String>, Option<String>, Option<i64>)> = c
            .prepare_cached("SELECT org, dst, n_flights FROM ref_schedule WHERE callsign = ?1 ORDER BY org, dst")
            .ok()?
            .query_map([callsign], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
            .ok()?
            .collect::<rusqlite::Result<_>>()
            .ok()?;
        schedule_chain(&legs)
    }

    fn airports(app: &App) -> Arc<HashMap<String, (f64, f64)>> {
        let empty = || Arc::new(HashMap::new());
        if !app.refdb.has(&["ref_airports"]) {
            return empty();
        }
        let Ok(c) = app.refdb.conn() else { return empty() };
        app.refdb
            .memo("estimate_airports", &c, |c| {
                let mut m = HashMap::new();
                let mut stmt = c.prepare(
                    "SELECT iata, lat, lon FROM ref_airports \
                     WHERE iata IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY rowid",
                )?;
                let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, f64>(1)?, r.get::<_, f64>(2)?)))?;
                for row in rows {
                    let (iata, lat, lon) = row?;
                    m.insert(iata, (lat, lon));
                }
                Ok(m)
            })
            .unwrap_or_else(|_| empty())
    }

    async fn estimates(&self, app: &App, now: f64) -> Vec<String> {
        let wanted = self.book.lock().unwrap().wanted(now);
        for flight in wanted {
            let hit = self.route_cache.lock().unwrap().get(&flight).is_some_and(|(at, _)| now - at < ROUTE_TTL_S);
            if hit {
                continue;
            }
            let route = self.resolve(app, &flight).await;
            let mut cache = self.route_cache.lock().unwrap();
            if cache.len() > ROUTE_CACHE_MAX {
                cache.clear();
            }
            cache.insert(flight, (now, route));
        }
        let airports = Estimator::airports(app);
        let cache = self.route_cache.lock().unwrap();
        let route = |cs: &str| cache.get(cs).map(|(_, r)| r.clone());
        self.book.lock().unwrap().estimates(now, &route, &airports)
    }

    /// The last night's measured accuracy of the method in use; a found
    /// score is kept ten minutes, the lack of one a minute.
    async fn accuracy(&self) -> Option<String> {
        let mut g = self.accuracy.lock().await;
        let now = now_s();
        if now - g.0 > if g.1.is_some() { 600.0 } else { 60.0 } {
            g.0 = now;
            if let Some(v) = self.read_accuracy().await {
                g.1 = Some(v);
            }
        }
        g.1.clone()
    }

    async fn read_accuracy(&self) -> Option<String> {
        let c = self.db.as_ref()?.get().await.ok()?;
        let row = c
            .as_ref()
            .unwrap()
            .query_opt("SELECT day, detail::text FROM estimate_scores ORDER BY day DESC LIMIT 1", &[])
            .await
            .ok()??;
        let day: chrono::NaiveDate = row.get(0);
        let detail: Value = serde_json::from_str(&row.get::<_, String>(1)).ok()?;
        let m = detail.get("methods").and_then(|m| m.get("converge")).cloned().unwrap_or(Value::Null);
        let get = |k: &str| m.get(k).cloned();
        let mut s = String::new();
        let mut o = Obj::new(&mut s);
        o.str("day", &day.format("%Y-%m-%d").to_string());
        crate::pyjson::write_value(o.key("n"), &get("n").unwrap_or(Value::from(0)));
        crate::pyjson::write_value(o.key("median_km"), &get("median_km").unwrap_or(Value::Null));
        crate::pyjson::write_value(o.key("by_gap_min"), &get("by_gap_min").unwrap_or(Value::Object(Default::default())));
        o.end();
        Some(s)
    }

    // ---- the book's memory across restarts ----------------------------------

    pub async fn restore(&self) {
        let Some(db) = &self.db else { return };
        let read = async {
            let g = db.get().await?;
            g.as_ref().unwrap().query_opt("SELECT value::text FROM live_state WHERE key = $1", &[&STATE_KEY]).await
        };
        match read.await {
            Ok(Some(row)) => {
                let items: Value = serde_json::from_str(&row.get::<_, String>(0)).unwrap_or(Value::Null);
                let n = self.book.lock().unwrap().restore(&items, now_s());
                if n > 0 {
                    eprintln!("estimates: {n} aircraft restored");
                }
            }
            Ok(None) => {}
            Err(e) => eprintln!("estimates: restore failed: {e}"),
        }
    }

    pub async fn save(&self) -> Result<(), tokio_postgres::Error> {
        let Some(db) = &self.db else { return Ok(()) };
        let items = self.book.lock().unwrap().dump();
        let g = db.get().await?;
        g.as_ref()
            .unwrap()
            .execute(
                "INSERT INTO live_state (key, value, updated_at) VALUES ($1, $2::text::json, now()) \
                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at",
                &[&STATE_KEY, &items],
            )
            .await?;
        Ok(())
    }

    /// Restore once, then save every SAVE_S.
    pub async fn keep(self: Arc<Estimator>) {
        self.restore().await;
        loop {
            tokio::time::sleep(Duration::from_secs(SAVE_S)).await;
            if let Err(e) = self.save().await {
                eprintln!("estimates: save failed: {e}");
            }
        }
    }
}

/// GET /v1/estimated
pub async fn estimated(State(app): State<Arc<App>>, req: Request) -> Response {
    let Some(est) = app.estimates.clone() else {
        return crate::proxy::forward(State(app), req).await.into_response();
    };
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "estimated", app.settings.aircraft_rate_limit) {
        return e.into_response();
    }
    let now = now_s();
    let listed = est.estimates(&app, now).await;
    let accuracy = est.accuracy().await;
    let mut out = String::with_capacity(160 + listed.iter().map(|s| s.len() + 1).sum::<usize>());
    let mut o = Obj::new(&mut out);
    o.f64("generated_at", now).str("method", "converge-to-destination");
    o.raw("accuracy", accuracy.as_deref().unwrap_or("null"));
    let list = o.key("aircraft");
    list.push('[');
    list.push_str(&listed.join(","));
    list.push(']');
    o.end();
    json(out, "public, max-age=10, s-maxage=15")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_modulo() {
        assert_eq!(py_mod(-10.0, 360.0), 350.0);
        assert_eq!(py_mod(370.0, 360.0), 10.0);
        assert_eq!(py_mod(-0.0, 360.0), 0.0);
        assert_eq!(angle_diff(350.0, 10.0), 20.0);
        assert_eq!(angle_diff(10.0, 190.0), 180.0);
    }

    #[test]
    fn schedule_chains() {
        let l = |o: &str, d: &str, n: i64| (Some(o.to_string()), Some(d.to_string()), Some(n));
        let s = |v: &[&str]| Some(v.iter().map(|x| Some(x.to_string())).collect::<Vec<_>>());
        assert_eq!(schedule_chain(&[l("SIN", "LHR", 3)]), s(&["SIN", "LHR"]));
        assert_eq!(schedule_chain(&[l("B", "C", 3), l("A", "B", 3)]), s(&["A", "B", "C"]));
        assert_eq!(schedule_chain(&[l("A", "B", 8), l("A", "C", 2)]), s(&["A", "B"]));
        assert_eq!(schedule_chain(&[l("A", "B", 6), l("A", "C", 4)]), None);
        assert_eq!(schedule_chain(&[l("A", "A", 6)]), None);
    }

    /// Replays a scenario (EST_SCENARIO) through the book and writes what
    /// it answers to EST_OUT, for comparison with the Python book fed the
    /// same (the parity script in the scratch bench).
    #[test]
    fn replays_a_scenario() {
        let (Ok(path), Ok(out)) = (std::env::var("EST_SCENARIO"), std::env::var("EST_OUT")) else { return };
        let sc: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        let airports: HashMap<String, (f64, f64)> = sc["airports"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), (v[0].as_f64().unwrap(), v[1].as_f64().unwrap())))
            .collect();
        let routes = sc["routes"].as_object().unwrap();
        let route = |cs: &str| Some(routes.get(cs).and_then(chain_of));
        let mut book = Book::default();
        let mut text = format!("restored {}\n", book.restore(&sc["restore"], sc["restore_now"].as_f64().unwrap()));
        for st in sc["steps"].as_array().unwrap() {
            let now = st["now"].as_f64().unwrap();
            if st.get("estimate").is_some() {
                text.push_str(&format!("[{}]\n", book.estimates(now, &route, &airports).join(",")));
            } else {
                let vals: Vec<Vals> = st["aircraft"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|a| crate::sky::parse_aircraft(&a.to_string()).unwrap())
                    .collect();
                book.observe(vals.iter(), now);
            }
        }
        text.push_str(&book.dump());
        text.push('\n');
        std::fs::write(out, text).unwrap();
    }
}
