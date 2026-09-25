//! The fleet tier: a second, private listener (NETWORKD_FLEET_BIND) for
//! one known client, the frames' backend. It serves `/fleet/v1/*` and
//! `/fleet/healthz` and nothing else; the public listener serves none of
//! it. Every `/fleet/v1/*` request carries `Authorization: Bearer
//! <NETWORKD_FLEET_TOKEN>`. No rate limits, no CORS, no edge caching:
//! every response is `Cache-Control: no-store`.
//!
//! Aircraft here are the public allowlist plus what the reference
//! snapshot and the routes artifact know about them (military flag,
//! type name and class, operator, build year, route, message source).
//! Enrichment reads memory and the local SQLite snapshot only: the
//! registry rows are remembered per hex for the snapshot's generation,
//! the type and airline tables are memoised whole, and the community
//! catalog (Postgres) is asked once per request at most, for callsigns
//! it has not answered in the last ten minutes.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::extract::{Path, Request, State};
use axum::http::{header, HeaderValue};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tower::Layer;

use crate::http::{json, ApiError, ApiResult};
use crate::live::{distance_nm, fresh, py_float};
use crate::pyjson::{round_to, write_float, write_str, write_value, Obj, Val};
use crate::sky::{Entry, Snapshot, F_FLIGHT};
use crate::state::{now_s, App};

/// The point query's radius cap, in nautical miles.
pub const MAX_RADIUS_NM: f64 = 250.0;
/// Callsigns one /fleet/v1/routes request may ask about.
const ROUTES: crate::catalog::RoutesLimits = crate::catalog::RoutesLimits { max_callsigns: 200, max_chars: 2800 };
/// Shorter tokens keep the tier off.
pub const MIN_TOKEN_LEN: usize = 32;
/// How long a catalog answer (or its silence) is reused. The catalog
/// takes new rows every ten minutes.
const CATALOG_TTL_S: f64 = 600.0;
const CATALOG_TIMEOUT: Duration = Duration::from_secs(2);
const IDENT_CACHE_MAX: usize = 200_000;
const CATALOG_CACHE_MAX: usize = 50_000;
const NO_STORE: &str = "no-store";
/// readsb's dbFlags and tar1090-db's flag digits: bit 0 is military.
const DBFLAG_MILITARY: i64 = 1;

/// What the registry says about one hex (ref_airframes).
#[derive(Clone, Debug, Default, PartialEq)]
struct Registry {
    type_code: Option<String>,
    operator_name: Option<String>,
    operator_icao: Option<String>,
    year: Option<i64>,
    /// tar1090-db's flag digits, one per bit, bit 0 first ("10": military)
    flags: Option<String>,
}

#[derive(Default)]
struct Idents {
    generation: u64,
    by_hex: HashMap<String, Option<Arc<Registry>>>,
}

/// Lookups the fleet tier remembers between requests.
#[derive(Default)]
pub struct Caches {
    idents: Mutex<Idents>,
    catalog: Mutex<HashMap<String, (f64, Option<Value>)>>,
}

/// One aircraft's enrichment; every field omitted when unknown.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Extra {
    pub mil: Option<bool>,
    pub type_name: Option<String>,
    pub class: Option<String>,
    pub operator: Option<String>,
    pub operator_icao: Option<String>,
    pub year: Option<i64>,
}

/// tar1090-db's flag digits ("10", "0001", ...): character i is bit i.
/// None when the value is not such a string.
pub fn military_from_digits(flags: &str) -> Option<bool> {
    let f = flags.trim();
    if f.is_empty() || !f.bytes().all(|b| b == b'0' || b == b'1') {
        return None;
    }
    Some(f.as_bytes()[0] == b'1')
}

fn nonempty(s: Option<String>) -> Option<String> {
    s.map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

fn str_val(v: &Option<Val>) -> Option<String> {
    match v {
        Some(Val::Str(s)) => nonempty(Some(s.to_string())),
        _ => None,
    }
}

/// The type table, whole: designator -> (name, category).
type Types = HashMap<String, (String, Option<String>)>;
/// Airline names by ICAO code.
type Airlines = HashMap<String, String>;

/// Enrich `picks` of `snap` from the reference snapshot. Blocking
/// (SQLite); local reads only.
fn enrich(app: &App, snap: &Snapshot, picks: &[u32]) -> Vec<Extra> {
    let mut out = vec![Extra::default(); picks.len()];
    let registry = registry_rows(app, snap, picks);
    let (mut types, mut airlines): (Option<Arc<Types>>, Option<Arc<Airlines>>) = (None, None);
    if app.refdb.has(&["ref_types"]) || app.refdb.has(&["ref_airlines"]) {
        if let Ok(c) = app.refdb.conn() {
            if app.refdb.has(&["ref_types"]) {
                types = app
                    .refdb
                    .memo("fleet_types", &c, |c| {
                        let mut stmt = c.prepare("SELECT designator, name, category FROM ref_types")?;
                        let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, (r.get(1)?, r.get(2)?))))?;
                        rows.collect::<rusqlite::Result<Types>>()
                    })
                    .map_err(|e| eprintln!("fleet: types: {e}"))
                    .ok();
            }
            if app.refdb.has(&["ref_airlines"]) {
                airlines = app
                    .refdb
                    .memo("fleet_airlines", &c, |c| {
                        let mut stmt = c.prepare("SELECT icao, name FROM ref_airlines")?;
                        let rows = stmt.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
                        rows.collect::<rusqlite::Result<Airlines>>()
                    })
                    .map_err(|e| eprintln!("fleet: airlines: {e}"))
                    .ok();
            }
        }
    }
    for (k, &i) in picks.iter().enumerate() {
        let e = &snap.entries[i as usize];
        let reg = registry.get(k).cloned().flatten();
        let x = &mut out[k];
        let from_db = e.hidden.db_flags.map(|f| f & DBFLAG_MILITARY != 0);
        let from_registry = reg.as_ref().and_then(|r| r.flags.as_deref()).and_then(military_from_digits);
        x.mil = match (from_db, from_registry) {
            (None, None) => None,
            (a, b) => Some(a.unwrap_or(false) || b.unwrap_or(false)),
        };
        // the registry's type first, the aircraft's own `t` where it is silent
        let type_code = reg.as_ref().and_then(|r| r.type_code.clone()).or_else(|| str_val(&e.vals[2]));
        if let (Some(code), Some(types)) = (type_code, &types) {
            if let Some((name, category)) = types.get(&code) {
                x.type_name = nonempty(Some(name.clone()));
                x.class = nonempty(category.clone());
            }
        }
        if let Some(r) = reg {
            x.operator_icao = r.operator_icao.clone();
            x.operator = r.operator_name.clone().or_else(|| {
                let icao = r.operator_icao.as_ref()?;
                nonempty(airlines.as_ref()?.get(icao).cloned())
            });
            x.year = r.year;
        }
    }
    out
}

/// ref_airframes rows for the picks' hexes, from the per-generation
/// cache, reading SQLite only for hexes not seen since the snapshot
/// changed.
fn registry_rows(app: &App, snap: &Snapshot, picks: &[u32]) -> Vec<Option<Arc<Registry>>> {
    let mut out = vec![None; picks.len()];
    if !app.refdb.has(&["ref_airframes"]) {
        return out;
    }
    let c = match app.refdb.conn() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("fleet: refdata: {e}");
            return out;
        }
    };
    let generation = c.generation();
    let hexes: Vec<String> = picks.iter().map(|&i| snap.entries[i as usize].hex.trim().to_lowercase()).collect();
    let mut todo = vec![];
    {
        let mut g = app.fleet.idents.lock().unwrap();
        if g.generation != generation {
            g.generation = generation;
            g.by_hex.clear();
        }
        for (k, h) in hexes.iter().enumerate() {
            match g.by_hex.get(h) {
                Some(hit) => out[k] = hit.clone(),
                None => todo.push(k),
            }
        }
    }
    if todo.is_empty() {
        return out;
    }
    let mut read = Vec::with_capacity(todo.len());
    let lookup = (|| -> rusqlite::Result<()> {
        let mut stmt = c.prepare_cached(
            "SELECT type_code, operator_name, operator_icao, year, flags FROM ref_airframes WHERE hex = ?1",
        )?;
        for &k in &todo {
            let mut rows = stmt.query([&hexes[k]])?;
            let row = match rows.next()? {
                Some(r) => Some(Arc::new(Registry {
                    type_code: nonempty(r.get(0)?),
                    operator_name: nonempty(r.get(1)?),
                    operator_icao: nonempty(r.get(2)?),
                    year: r.get(3)?,
                    flags: nonempty(r.get(4)?),
                })),
                None => None,
            };
            read.push((k, row));
        }
        Ok(())
    })();
    if let Err(e) = lookup {
        eprintln!("fleet: ref_airframes: {e}");
    }
    let mut g = app.fleet.idents.lock().unwrap();
    let same = g.generation == generation;
    if same && g.by_hex.len() + read.len() > IDENT_CACHE_MAX {
        g.by_hex.clear();
    }
    for (k, row) in read {
        if same {
            g.by_hex.insert(hexes[k].clone(), row.clone());
        }
        out[k] = row;
    }
    out
}

/// Routes for the callsigns: the observed routes artifact first, the
/// community catalog where it is silent (as /v1/routes). One catalog
/// query at most, for what the cache does not hold; a slow or failed
/// catalog leaves those routes out.
async fn routes_for(app: &App, callsigns: &[Option<String>]) -> HashMap<String, Value> {
    let mut out = HashMap::new();
    let mut asked: Vec<String> = vec![];
    let mut seen = HashSet::new();
    for cs in callsigns.iter().flatten() {
        let n = cs.trim().to_uppercase();
        if (2..=12).contains(&n.chars().count()) && crate::catalog::alnum(&n) && seen.insert(n.clone()) {
            asked.push(n);
        }
    }
    let routes_up = app.routes.available();
    let usable = |v: &Value| v.as_array().is_some_and(|a| !a.is_empty());
    let mut missing = vec![];
    for n in asked {
        match app.routes.get(&n).filter(|_| routes_up) {
            Some(r) if usable(&r) => {
                out.insert(n, r);
            }
            _ => missing.push(n),
        }
    }
    if missing.is_empty() || app.db.is_none() {
        return out;
    }
    let now = now_s();
    let mut need = vec![];
    {
        let cache = app.fleet.catalog.lock().unwrap();
        for n in missing {
            match cache.get(&n) {
                Some((at, r)) if now - at < CATALOG_TTL_S => {
                    if let Some(r) = r.clone().filter(usable) {
                        out.insert(n, r);
                    }
                }
                _ => need.push(n),
            }
        }
    }
    if need.is_empty() {
        return out;
    }
    let found = match tokio::time::timeout(CATALOG_TIMEOUT, crate::catalog::catalog_routes(app, &need)).await {
        Ok(Ok(found)) => found,
        Ok(Err(e)) => {
            eprintln!("fleet: catalog: {e}");
            return out;
        }
        Err(_) => {
            eprintln!("fleet: catalog: timed out");
            return out;
        }
    };
    let mut cache = app.fleet.catalog.lock().unwrap();
    if cache.len() + need.len() > CATALOG_CACHE_MAX {
        cache.clear();
    }
    for n in need {
        let r = found.get(&n).cloned();
        if let Some(r) = r.clone().filter(usable) {
            out.insert(n.clone(), r);
        }
        cache.insert(n, (now, r));
    }
    out
}

/// One aircraft as the fleet reads it: the served fields, `dst` when
/// there is a reference point, then the enrichment.
fn write_aircraft(buf: &mut String, e: &Entry, dst: Option<f64>, x: &Extra, route: Option<&Value>) {
    let j = &e.json;
    buf.push_str(&j[..j.len() - 1]);
    let mut first = j.len() <= 2;
    let mut key = |buf: &mut String, k: &str| {
        if !first {
            buf.push(',');
        }
        first = false;
        write_str(buf, k);
        buf.push(':');
    };
    if let Some(d) = dst {
        key(buf, "dst");
        write_float(buf, d);
    }
    if let Some(m) = x.mil {
        key(buf, "mil");
        buf.push_str(if m { "true" } else { "false" });
    }
    for (k, v) in [
        ("type_name", &x.type_name),
        ("class", &x.class),
        ("operator", &x.operator),
        ("operator_icao", &x.operator_icao),
    ] {
        if let Some(v) = v {
            key(buf, k);
            write_str(buf, v);
        }
    }
    if let Some(y) = x.year {
        key(buf, "year");
        buf.push_str(&y.to_string());
    }
    if let Some(r) = route {
        key(buf, "route");
        write_value(buf, r);
    }
    if let Some(s) = e.hidden.source {
        key(buf, "source");
        write_str(buf, s);
    }
    buf.push('}');
}

/// The `ac` array for `picks` (index into the snapshot, distance).
async fn aircraft_array(app: &Arc<App>, snap: &Arc<Snapshot>, picks: &[(u32, Option<f64>)]) -> Result<String, ApiError> {
    let idx: Vec<u32> = picks.iter().map(|p| p.0).collect();
    let (a, s, i) = (app.clone(), snap.clone(), idx.clone());
    let extras = tokio::task::spawn_blocking(move || enrich(&a, &s, &i))
        .await
        .map_err(|_| ApiError::new(500, "internal_error", "internal error"))?;
    let callsigns: Vec<Option<String>> =
        idx.iter().map(|&i| str_val(&snap.entries[i as usize].vals[F_FLIGHT])).collect();
    let routes = routes_for(app, &callsigns).await;
    let mut buf = String::with_capacity(picks.iter().map(|&(i, _)| snap.entries[i as usize].json.len() + 200).sum::<usize>() + 2);
    buf.push('[');
    for (k, &(i, d)) in picks.iter().enumerate() {
        if k > 0 {
            buf.push(',');
        }
        let route = callsigns[k].as_ref().and_then(|c| routes.get(&c.trim().to_uppercase()));
        write_aircraft(&mut buf, &snap.entries[i as usize], d, &extras[k], route);
    }
    buf.push(']');
    Ok(buf)
}

/// GET /fleet/v1/point/{lat}/{lon}/{radius_nm}: /v2/point's envelope,
/// enriched, unthrottled.
pub async fn point(State(app): State<Arc<App>>, Path((lat, lon, radius)): Path<(String, String, String)>) -> ApiResult {
    for (name, v) in [("lat", &lat), ("lon", &lon), ("radius", &radius)] {
        if py_float(v).is_none() {
            return Err(ApiError::new(
                422,
                "invalid_request",
                format!("{name}: Input should be a valid number, unable to parse string as a number"),
            ));
        }
    }
    let (lat, lon, radius) = (py_float(&lat).unwrap(), py_float(&lon).unwrap(), py_float(&radius).unwrap());
    if !((-90.0..=90.0).contains(&lat) && (-180.0..=180.0).contains(&lon)) {
        return Err(ApiError::new(422, "invalid_request", "invalid coordinates"));
    }
    if radius.is_nan() || radius <= 0.0 {
        return Err(ApiError::new(422, "invalid_request", "invalid radius"));
    }
    let radius = radius.min(MAX_RADIUS_NM);
    let started = Instant::now();
    let snap = fresh(&app)?;
    let mut hits: Vec<(u32, Option<f64>)> = vec![];
    for (i, e) in snap.entries.iter().enumerate() {
        let (Some(alat), Some(alon)) = (e.lat, e.lon) else { continue };
        let d = distance_nm(lat, lon, alat, alon);
        if d <= radius {
            hits.push((i as u32, Some(round_to(d, 1))));
        }
    }
    hits.sort_by(|a, b| a.1.unwrap().total_cmp(&b.1.unwrap()));
    let ac = aircraft_array(&app, &snap, &hits).await?;
    let mut s = String::with_capacity(ac.len() + 128);
    let mut o = Obj::new(&mut s);
    o.raw("ac", &ac)
        .str("msg", "No error")
        .f64("now", snap.generated_at)
        .int("total", hits.len() as i64)
        .f64("ctime", snap.generated_at)
        .f64("ptime", round_to(started.elapsed().as_secs_f64() * 1000.0, 3));
    o.end();
    Ok(json(s, NO_STORE))
}

/// GET /fleet/v1/callsign/{callsign}: the aircraft live under a
/// callsign now (case-insensitive, trimmed), in snapshot order.
pub async fn callsign(State(app): State<Arc<App>>, Path(asked): Path<String>) -> ApiResult {
    let want = asked.trim().to_uppercase();
    if want.is_empty() || want.chars().count() > 16 {
        return Err(ApiError::new(422, "invalid_request", "invalid callsign"));
    }
    let snap = fresh(&app)?;
    let picks: Vec<(u32, Option<f64>)> = snap
        .entries
        .iter()
        .enumerate()
        .filter(|(_, e)| str_val(&e.vals[F_FLIGHT]).is_some_and(|f| f.to_uppercase() == want))
        .map(|(i, _)| (i as u32, None))
        .collect();
    let ac = aircraft_array(&app, &snap, &picks).await?;
    let mut s = String::with_capacity(ac.len() + 48);
    let mut o = Obj::new(&mut s);
    o.raw("ac", &ac).f64("now", snap.generated_at);
    o.end();
    Ok(json(s, NO_STORE))
}

/// GET /fleet/v1/routes?cs=A,B: /v1/routes, unthrottled, up to 200.
pub async fn routes(State(app): State<Arc<App>>, req: Request) -> Response {
    crate::catalog::routes_body(&app, req.uri().query(), &ROUTES, None).await
}

/// GET /fleet/healthz: the snapshot's age; 503 when it is stale, by the
/// public rule. Needs no token.
pub async fn healthz(State(app): State<Arc<App>>) -> Response {
    let snap = app.snapshot();
    let age = if snap.generated_at > 0.0 { Some(round_to((now_s() - snap.generated_at).max(0.0), 1)) } else { None };
    if !snap.fresh(app.settings.stale_after_s) {
        let detail = match age {
            Some(a) => format!("sky data unavailable (snapshot {a} s old)"),
            None => "sky data unavailable (no snapshot yet)".to_string(),
        };
        return ApiError::new(503, "stale_snapshot", detail).header("Retry-After", "5").into_response();
    }
    let mut s = String::with_capacity(96);
    let mut o = Obj::new(&mut s);
    o.raw("ok", "true")
        .f64("age_s", age.unwrap_or(0.0))
        .f64("generated_at", snap.generated_at)
        .int("aircraft", snap.count() as i64);
    o.end();
    json(s, NO_STORE)
}

/// Equal tokens, compared in time independent of where they differ
/// (both sides hashed first, so length leaks nothing either).
pub fn token_matches(given: &str, expected: &str) -> bool {
    if expected.len() < MIN_TOKEN_LEN {
        return false;
    }
    let (a, b) = (Sha256::digest(given.as_bytes()), Sha256::digest(expected.as_bytes()));
    a.iter().zip(b.iter()).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn unauthorized(detail: &str) -> Response {
    ApiError::new(401, "unauthorized", detail).header("WWW-Authenticate", "Bearer").into_response()
}

/// Every /fleet/v1 request carries the token; then every response is
/// no-store.
async fn guard(State(app): State<Arc<App>>, req: Request, next: Next) -> Response {
    let path = req.uri().path();
    if path == "/fleet/v1" || path.starts_with("/fleet/v1/") {
        let given = req
            .headers()
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.split_once(' '))
            .filter(|(scheme, _)| scheme.eq_ignore_ascii_case("bearer"))
            .map(|(_, t)| t.trim());
        match given {
            None => return unauthorized("bearer token required"),
            Some(t) if !token_matches(t, &app.settings.fleet_token) => return unauthorized("invalid token"),
            Some(_) => {}
        }
    }
    let mut r = next.run(req).await;
    r.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static(NO_STORE));
    r
}

async fn not_found() -> ApiError {
    ApiError::new(404, "not_found", "Not Found")
}

pub fn router(app: Arc<App>) -> Router {
    let routes = Router::new()
        .route("/fleet/healthz", get(healthz))
        .route("/fleet/v1/point/{lat}/{lon}/{radius}", get(point))
        .route("/fleet/v1/callsign/{callsign}", get(callsign))
        .route("/fleet/v1/routes", get(routes))
        .fallback(not_found)
        .method_not_allowed_fallback(crate::http::method_not_allowed)
        .with_state(app.clone());
    // the guard wraps the router, so it sees unknown paths too
    Router::new().fallback_service(axum::middleware::from_fn_with_state(app, guard).layer(routes))
}

/// Start the fleet listener when it is configured; a bad configuration
/// keeps it off and says so, and never takes the public API down.
pub async fn start(app: &Arc<App>) {
    let s = &app.settings;
    if s.fleet_bind.is_empty() {
        return;
    }
    if s.fleet_token.len() < MIN_TOKEN_LEN {
        eprintln!("fleet tier off: NETWORKD_FLEET_TOKEN must be at least {MIN_TOKEN_LEN} characters");
        return;
    }
    let listener = match tokio::net::TcpListener::bind(&s.fleet_bind).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("fleet tier off: {}: {e}", s.fleet_bind);
            return;
        }
    };
    eprintln!("fleet tier listening on {}", s.fleet_bind);
    let r = router(app.clone());
    tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, r).await {
            eprintln!("fleet tier stopped: {e}");
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::settings::Settings;
    use axum::body::Body;
    use crate::sky::parse_aircraft_full;
    use std::io::Write;
    use tower::Service;

    const TOKEN: &str = "0123456789abcdef0123456789abcdef-test";

    fn scratch(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("networkd-fleet-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn refdata(path: &std::path::Path) {
        let c = rusqlite::Connection::open(path).unwrap();
        c.execute_batch(
            "CREATE TABLE ref_airframes (hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, operator_name TEXT,
                 operator_norm TEXT, operator_icao TEXT, year INTEGER, flags TEXT, source TEXT, updated_at TEXT);
             CREATE TABLE ref_types (designator TEXT PRIMARY KEY, name TEXT, category TEXT);
             CREATE TABLE ref_airlines (icao TEXT PRIMARY KEY, iata TEXT, name TEXT);
             INSERT INTO ref_airframes VALUES ('ae1234', '64-13199', 'C17', NULL, NULL, NULL, 2001, '10', 'tar1090', '');
             INSERT INTO ref_airframes VALUES ('76cd01', '9V-SWA', 'B77W', NULL, NULL, 'SIA', 2007, '00', 'tar1090', '');
             INSERT INTO ref_types VALUES ('C17', 'Boeing C-17 Globemaster III', 'wide');
             INSERT INTO ref_types VALUES ('B77W', 'Boeing 777-300ER', 'wide');
             INSERT INTO ref_types VALUES ('A320', 'Airbus A320', 'narrow');
             INSERT INTO ref_airlines VALUES ('SIA', 'SQ', 'Singapore Airlines');",
        )
        .unwrap();
    }

    fn routes_file(path: &std::path::Path) {
        let f = std::fs::File::create(path).unwrap();
        let mut gz = flate2::write::GzEncoder::new(f, flate2::Compression::fast());
        gz.write_all(br#"{"SIA322":["WSSS","EGLL"],"SIA1":["WSSS","WMKK","WSSS"]}"#).unwrap();
        gz.finish().unwrap();
    }

    fn app(name: &str) -> Arc<App> {
        let d = scratch(name);
        refdata(&d.join("refdata.sqlite"));
        routes_file(&d.join("routes.json.gz"));
        let mut s = Settings::from_env();
        s.refdata_path = d.join("refdata.sqlite").to_string_lossy().into();
        s.routes_path = d.join("routes.json.gz").to_string_lossy().into();
        s.legs_path = d.join("legs.db").to_string_lossy().into();
        s.gaps_path = d.join("gaps.json.gz").to_string_lossy().into();
        s.boards_path = d.join("boards.db").to_string_lossy().into();
        s.database_url = String::new();
        s.fallback = String::new();
        s.squawks = false;
        s.stations = false;
        s.estimates = false;
        s.nat = false;
        s.fleet_bind = "127.0.0.1:0".into();
        s.fleet_token = TOKEN.into();
        let app = App::new(s);
        app.routes.refresh();
        let rows = [
            r#"{"hex":"76cd01","flight":"SIA322  ","t":"B77W","r":"9V-SWA","lat":1.40,"lon":103.90,"alt_baro":12000,"type":"adsb_icao"}"#,
            r#"{"hex":"ae1234","flight":"RCH123","lat":1.36,"lon":103.83,"alt_baro":"ground","type":"mlat","dbFlags":0}"#,
            r#"{"hex":"abcdef","flight":"sia322","t":"A320","lat":5.0,"lon":110.0}"#,
            r#"{"hex":"c0ffee","lat":1.35,"lon":103.82,"dbFlags":1,"type":"tisb_icao"}"#,
            r#"{"hex":"123456","flight":"NOPOS1"}"#,
        ];
        let entries = rows
            .iter()
            .map(|r| {
                let (v, h) = parse_aircraft_full(r).unwrap();
                Entry::with_hidden(v, h)
            })
            .collect();
        app.set_snapshot(Arc::new(Snapshot::new(now_s(), entries)));
        app
    }

    async fn call(r: &mut Router, path: &str, token: Option<&str>) -> (u16, Response<Body>, Value) {
        let mut b = axum::http::Request::builder().uri(path);
        if let Some(t) = token {
            b = b.header("Authorization", format!("Bearer {t}"));
        }
        let resp = r.call(b.body(Body::empty()).unwrap()).await.unwrap();
        let status = resp.status().as_u16();
        let (parts, body) = resp.into_parts();
        let bytes = axum::body::to_bytes(body, 1 << 20).await.unwrap();
        let v = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, Response::from_parts(parts, Body::empty()), v)
    }

    #[test]
    fn flag_digits() {
        assert_eq!(military_from_digits("10"), Some(true));
        assert_eq!(military_from_digits("1"), Some(true));
        assert_eq!(military_from_digits("0001"), Some(false));
        assert_eq!(military_from_digits("00"), Some(false));
        assert_eq!(military_from_digits(""), None);
        assert_eq!(military_from_digits("x1"), None);
    }

    #[test]
    fn tokens() {
        assert!(token_matches(TOKEN, TOKEN));
        assert!(!token_matches("wrong", TOKEN));
        assert!(!token_matches("", TOKEN));
        // a short configured token never matches, not even itself
        assert!(!token_matches("short", "short"));
    }

    #[tokio::test]
    async fn every_v1_request_needs_the_token() {
        let mut r = router(app("auth"));
        for path in ["/fleet/v1/point/1.35/103.82/50", "/fleet/v1/callsign/SIA322", "/fleet/v1/routes?cs=SIA322", "/fleet/v1/nope"] {
            let (status, resp, v) = call(&mut r, path, None).await;
            assert_eq!(status, 401, "{path}");
            assert_eq!(v["error"], "unauthorized");
            assert!(v["detail"].is_string());
            assert_eq!(resp.headers()["cache-control"], "no-store");
            let (status, _, v) = call(&mut r, path, Some("not-the-token")).await;
            assert_eq!((status, v["error"].as_str()), (401, Some("unauthorized")), "{path}");
        }
        let (status, _, _) = call(&mut r, "/fleet/v1/nope", Some(TOKEN)).await;
        assert_eq!(status, 404);
        // the scheme is case-insensitive
        let req = axum::http::Request::builder()
            .uri("/fleet/v1/callsign/SIA322")
            .header("Authorization", format!("bearer {TOKEN}"))
            .body(Body::empty())
            .unwrap();
        assert_eq!(r.call(req).await.unwrap().status().as_u16(), 200);
    }

    #[tokio::test]
    async fn point_is_enriched_nearest_first() {
        let mut r = router(app("point"));
        let (status, resp, v) = call(&mut r, "/fleet/v1/point/1.3521/103.8198/400", Some(TOKEN)).await;
        assert_eq!(status, 200);
        assert_eq!(resp.headers()["cache-control"], "no-store");
        assert!(resp.headers().get("access-control-allow-origin").is_none());
        for k in ["msg", "now", "total", "ctime", "ptime"] {
            assert!(v.get(k).is_some(), "{k}");
        }
        let ac = v["ac"].as_array().unwrap();
        let hexes: Vec<&str> = ac.iter().map(|a| a["hex"].as_str().unwrap()).collect();
        // 400 nm is capped at 250: the aircraft off Borneo is out; no position, no entry
        assert_eq!(hexes, ["c0ffee", "ae1234", "76cd01"]);
        assert_eq!(v["total"], 3);
        let dsts: Vec<f64> = ac.iter().map(|a| a["dst"].as_f64().unwrap()).collect();
        assert!(dsts.windows(2).all(|w| w[0] <= w[1]));

        // military by the registry's flag digits, though readsb's dbFlags say 0
        let mil = &ac[1];
        assert_eq!(mil["mil"], true);
        assert_eq!(mil["alt_baro"], "ground");
        assert_eq!(mil["type_name"], "Boeing C-17 Globemaster III");
        assert_eq!(mil["class"], "wide");
        assert_eq!(mil["year"], 2001);
        assert_eq!(mil["source"], "mlat");
        assert!(mil.get("operator").is_none() && mil.get("route").is_none());

        // military by readsb's dbFlags alone; nothing else known
        let tisb = &ac[0];
        assert_eq!(tisb["mil"], true);
        assert_eq!(tisb["source"], "tisb");
        assert!(tisb.get("type_name").is_none() && tisb.get("year").is_none());

        let sq = &ac[2];
        assert_eq!(sq["mil"], false);
        assert_eq!(sq["flight"], "SIA322");
        assert_eq!(sq["type_name"], "Boeing 777-300ER");
        assert_eq!(sq["class"], "wide");
        assert_eq!(sq["operator"], "Singapore Airlines");
        assert_eq!(sq["operator_icao"], "SIA");
        assert_eq!(sq["year"], 2007);
        assert_eq!(sq["route"], serde_json::json!(["WSSS", "EGLL"]));
        assert_eq!(sq["source"], "adsb");
        // the allowlist first, in its order, then dst, then the enrichment
        let keys: Vec<&String> = sq.as_object().unwrap().keys().collect();
        assert_eq!(
            keys,
            ["hex", "flight", "t", "r", "lat", "lon", "alt_baro", "dst", "mil", "type_name", "class", "operator",
             "operator_icao", "year", "route", "source"]
        );

        let (status, _, v) = call(&mut r, "/fleet/v1/point/91/0/10", Some(TOKEN)).await;
        assert_eq!((status, v["error"].as_str()), (422, Some("invalid_request")));
    }

    #[tokio::test]
    async fn callsign_finds_every_aircraft_under_it() {
        let mut r = router(app("callsign"));
        let (status, _, v) = call(&mut r, "/fleet/v1/callsign/%20sia322%20", Some(TOKEN)).await;
        assert_eq!(status, 200);
        let ac = v["ac"].as_array().unwrap();
        let hexes: Vec<&str> = ac.iter().map(|a| a["hex"].as_str().unwrap()).collect();
        assert_eq!(hexes, ["76cd01", "abcdef"]);
        assert!(ac[0].get("dst").is_none());
        // the live `t` names the type where the registry has no row
        assert_eq!(ac[1]["type_name"], "Airbus A320");
        assert_eq!(ac[1]["class"], "narrow");
        assert_eq!(ac[1]["route"], serde_json::json!(["WSSS", "EGLL"]));
        assert!(v["now"].as_f64().unwrap() > 0.0);
        let (status, _, v) = call(&mut r, "/fleet/v1/callsign/NOBODY", Some(TOKEN)).await;
        assert_eq!(status, 200);
        assert_eq!(v["ac"], serde_json::json!([]));
    }

    #[tokio::test]
    async fn routes_take_two_hundred() {
        let mut r = router(app("routes"));
        let (status, _, v) = call(&mut r, "/fleet/v1/routes?cs=sia322,SIA1,XYZ9", Some(TOKEN)).await;
        assert_eq!(status, 200);
        assert_eq!(v["routes"]["SIA322"], serde_json::json!(["WSSS", "EGLL"]));
        assert_eq!(v["routes"]["SIA1"], serde_json::json!(["WSSS", "WMKK", "WSSS"]));
        assert_eq!(v["routes"]["XYZ9"], Value::Null);
        let many: Vec<String> = (0..200).map(|i| format!("TST{i}")).collect();
        let (status, _, v) = call(&mut r, &format!("/fleet/v1/routes?cs={}", many.join(",")), Some(TOKEN)).await;
        assert_eq!(status, 200);
        assert_eq!(v["routes"].as_object().unwrap().len(), 200);
        let (status, _, v) = call(&mut r, &format!("/fleet/v1/routes?cs={},TST200", many.join(",")), Some(TOKEN)).await;
        assert_eq!((status, v["detail"].as_str()), (422, Some("1 to 200 callsigns, 2-12 alphanumerics each")));
    }

    #[tokio::test]
    async fn healthz_tells_the_age_and_goes_503_when_stale() {
        let a = app("health");
        let mut r = router(a.clone());
        let (status, resp, v) = call(&mut r, "/fleet/healthz", None).await;
        assert_eq!(status, 200);
        assert_eq!(v["ok"], true);
        assert!(v["age_s"].as_f64().unwrap() < 5.0);
        assert_eq!(v["aircraft"], 5);
        assert_eq!(resp.headers()["cache-control"], "no-store");
        a.set_snapshot(Arc::new(Snapshot::new(now_s() - 120.0, vec![])));
        let (status, _, v) = call(&mut r, "/fleet/healthz", None).await;
        assert_eq!((status, v["error"].as_str()), (503, Some("stale_snapshot")));
        let (status, _, v) = call(&mut r, "/fleet/v1/point/1.35/103.82/50", Some(TOKEN)).await;
        assert_eq!((status, v["error"].as_str()), (503, Some("stale_snapshot")));
    }

    #[tokio::test]
    async fn the_public_listener_serves_no_fleet_route() {
        let mut public = crate::router(app("public"));
        for path in ["/fleet/healthz", "/fleet/v1/point/1.35/103.82/50", "/fleet/v1/routes?cs=SIA322", "/fleet"] {
            for token in [None, Some(TOKEN)] {
                let (status, _, v) = call(&mut public, path, token).await;
                assert_eq!((status, v["error"].as_str()), (404, Some("not_found")), "{path}");
            }
        }
        // and the public point carries none of the enrichment
        let (status, _, v) = call(&mut public, "/v2/point/1.3521/103.8198/50", None).await;
        assert_eq!(status, 200);
        for a in v["ac"].as_array().unwrap() {
            for k in ["mil", "type_name", "class", "operator", "route", "source", "year"] {
                assert!(a.get(k).is_none(), "{k}");
            }
        }
    }
}
