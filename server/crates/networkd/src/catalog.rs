//! Routes for callsigns (the Python service's /v1/routes): the observed
//! routes artifact first, the community catalog in force where it is
//! silent. The catalog changes during the day (the contributions pull
//! files approved answers every ten minutes), so it is read from
//! Postgres, not from the nightly snapshot.

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Request, State};
use axum::response::{IntoResponse, Response};
use serde_json::Value;

use crate::http::{client_ip, json, peer_of, query_param, throttle, ApiError};
use crate::pyjson::{write_str, write_value};
use rusqlite::OptionalExtension;

use crate::state::App;

pub const CACHE: &str = "public, s-maxage=3600";

/// Every history source is dark: a fact about our ops, not the world.
pub fn dark() -> ApiError {
    ApiError::new(503, "artifact_unavailable", "history artifacts are not loaded").header("Retry-After", "300")
}

/// The catalog rows in force today for `callsigns`, as route chains
/// [origin, *via, dest]; the latest valid_from wins. From Postgres, or
/// from the snapshot on an instance without it.
pub async fn catalog_routes(app: &App, callsigns: &[String]) -> Result<HashMap<String, Value>, String> {
    let mut out = HashMap::new();
    if callsigns.is_empty() {
        return Ok(out);
    }
    let today = chrono::Utc::now().date_naive();
    let chain = |origin: String, via: Option<String>, dest: String| {
        let mut chain = vec![Value::String(origin)];
        if let Some(Value::Array(via)) = via.and_then(|t| serde_json::from_str(&t).ok()) {
            chain.extend(via);
        }
        chain.push(Value::String(dest));
        Value::Array(chain)
    };
    let Some(db) = &app.db else {
        if !app.refdb.has(&["route_catalog"]) {
            return Ok(out);
        }
        let c = app.refdb.conn().map_err(|e| e.to_string())?;
        let day = today.format("%Y-%m-%d").to_string();
        let mut stmt = c
            .prepare_cached(
                "SELECT origin, via, dest FROM route_catalog WHERE callsign = ?1 AND valid_from <= ?2 \
                 AND (valid_to IS NULL OR valid_to > ?2) ORDER BY valid_from DESC, rowid LIMIT 1",
            )
            .map_err(|e| e.to_string())?;
        for cs in callsigns {
            let row = stmt
                .query_row(rusqlite::params![cs, day], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
                .optional()
                .map_err(|e| e.to_string())?;
            if let Some((o, v, d)) = row {
                out.insert(cs.clone(), chain(o, v, d));
            }
        }
        return Ok(out);
    };
    let g = db.get().await.map_err(|e| e.to_string())?;
    let rows = g
        .as_ref()
        .unwrap()
        .query(
            "SELECT DISTINCT ON (callsign) callsign, origin, via::text, dest FROM route_catalog \
             WHERE callsign = ANY($1) AND valid_from <= $2 AND (valid_to IS NULL OR valid_to > $2) \
             ORDER BY callsign, valid_from DESC",
            &[&callsigns, &today],
        )
        .await
        .map_err(|e| e.to_string())?;
    for r in rows {
        out.insert(r.get(0), chain(r.get(1), r.get(2), r.get(3)));
    }
    Ok(out)
}

/// Whether this instance can answer from the catalog: Postgres, or the
/// snapshot's copy of it.
pub fn catalog_here(app: &App) -> bool {
    app.db.is_some() || app.refdb.has(&["route_catalog"])
}

/// Python's `str.isalnum` for the characters a callsign may hold.
pub fn alnum(s: &str) -> bool {
    !s.is_empty() && s.chars().all(char::is_alphanumeric)
}

/// How many callsigns one /v1/routes request may ask about.
pub struct RoutesLimits {
    pub max_callsigns: usize,
    pub max_chars: usize,
}

pub const PUBLIC_ROUTES: RoutesLimits = RoutesLimits { max_callsigns: 80, max_chars: 1100 };

/// GET /v1/routes?cs=A,B,...
pub async fn routes_bulk(State(app): State<Arc<App>>, req: Request) -> Response {
    if !catalog_here(&app) {
        return crate::proxy::forward(State(app), req).await.into_response();
    }
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    routes_body(&app, req.uri().query(), &PUBLIC_ROUTES, Some(&ip)).await
}

/// The /v1/routes body for the query `q`; `ip` set: rate limited.
pub async fn routes_body(app: &App, q: Option<&str>, limits: &RoutesLimits, ip: Option<&str>) -> Response {
    // the query's own checks come before the rate limit, as FastAPI's do
    let Some(cs) = query_param(q, "cs") else {
        return ApiError::new(422, "invalid_request", "query.cs: Field required").into_response();
    };
    let len = cs.chars().count();
    if len < 2 {
        return ApiError::new(422, "invalid_request", "query.cs: String should have at least 2 characters").into_response();
    }
    if len > limits.max_chars {
        return ApiError::new(
            422,
            "invalid_request",
            format!("query.cs: String should have at most {} characters", limits.max_chars),
        )
        .into_response();
    }
    if let Some(ip) = ip {
        if let Err(e) = throttle(app, ip, "routes", app.settings.routes_rate_limit) {
            return e.into_response();
        }
    }
    let mut names: Vec<String> = vec![];
    for c in cs.split(',') {
        let c = c.trim();
        if c.is_empty() {
            continue;
        }
        let n = c.to_uppercase();
        if !names.contains(&n) {
            names.push(n);
        }
    }
    if names.is_empty()
        || names.len() > limits.max_callsigns
        || names.iter().any(|n| !(2..=12).contains(&n.chars().count()) || !alnum(n))
    {
        return ApiError::new(
            422,
            "invalid_request",
            format!("1 to {} callsigns, 2-12 alphanumerics each", limits.max_callsigns),
        )
        .into_response();
    }
    let routes_up = app.routes.available();
    if !routes_up && !app.legs.available() {
        return dark().into_response();
    }
    let mut found: Vec<Option<Value>> = names.iter().map(|n| if routes_up { app.routes.get(n) } else { None }).collect();
    let missing: Vec<String> = names.iter().zip(&found).filter(|(_, r)| r.is_none()).map(|(n, _)| n.clone()).collect();
    match catalog_routes(app, &missing).await {
        Ok(mut cat) => {
            for (n, r) in names.iter().zip(found.iter_mut()) {
                if r.is_none() {
                    *r = cat.remove(n);
                }
            }
        }
        Err(e) => {
            eprintln!("routes: catalog: {e}");
            return ApiError::new(500, "internal_error", "internal error").into_response();
        }
    }
    let mut out = String::with_capacity(32 + names.len() * 40);
    out.push_str("{\"routes\":{");
    for (i, (n, r)) in names.iter().zip(&found).enumerate() {
        if i > 0 {
            out.push(',');
        }
        write_str(&mut out, n);
        out.push(':');
        match r {
            Some(v) => write_value(&mut out, v),
            None => out.push_str("null"),
        }
    }
    out.push_str("}}");
    json(out, CACHE)
}

// ---- /v1/flights/{callsign} ----------------------------------------------------

/// One inferred-schedule row, as the flight page reads it.
#[derive(Clone, Debug)]
struct Sched {
    flight: Option<String>,
    source: Option<String>,
    org: Option<String>,
    dst: Option<String>,
    dep_min: Option<i64>,
    arr_min: Option<i64>,
    type_code: Option<String>,
    n_flights: Option<i64>,
    weekdays: Option<String>,
}

/// ORDER BY n_flights DESC as Postgres runs it: NULLs first.
fn by_flights_desc(a: &Option<i64>, b: &Option<i64>) -> std::cmp::Ordering {
    use std::cmp::Ordering::*;
    match (a, b) {
        (None, None) => Equal,
        (None, _) => Less,
        (_, None) => Greater,
        (Some(x), Some(y)) => y.cmp(x),
    }
}

fn hhmm(minute: Option<i64>) -> Value {
    match minute {
        Some(m) => Value::String(format!("{:02}:{:02}", m.div_euclid(60), m.rem_euclid(60))),
        None => Value::Null,
    }
}

fn opt(s: &Option<String>) -> Value {
    s.clone().map_or(Value::Null, Value::String)
}

/// What the snapshot and the flight log say about a flight number:
/// (operating callsign, marketed number, schedule rows, flight log).
type FlightFacts = (String, Option<String>, Vec<Sched>, Option<Value>);

fn flight_facts(app: &App, asked: &str) -> rusqlite::Result<FlightFacts> {
    let mut callsign = asked.to_string();
    let mut marketed = None;
    let mut scheduled = vec![];
    if app.refdb.has(&["ref_schedule"]) {
        let c = app.refdb.conn()?;
        // a marketed number (LH996) resolves to the callsign that flies it
        // when a board has named the service; rows in heap order, then
        // Postgres's sort, whose ties the first row settles
        let mut hits: Vec<(String, Option<i64>)> = c
            .prepare_cached(
                "SELECT callsign, n_flights FROM ref_schedule WHERE flight = ?1 AND callsign <> ?1 ORDER BY rowid",
            )?
            .query_map([asked], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<rusqlite::Result<_>>()?;
        crate::pgsort::sort(&mut hits, &|a: &(String, Option<i64>), b: &(String, Option<i64>)| by_flights_desc(&a.1, &b.1));
        if let Some((operating, _)) = hits.into_iter().next() {
            marketed = Some(asked.to_string());
            callsign = operating;
        }
        let names = [callsign.as_str(), marketed.as_deref().unwrap_or(callsign.as_str())];
        let number = marketed.as_deref().unwrap_or(callsign.as_str());
        let wd = crate::boards::weekdays_col(&app.refdb);
        let mut rows: Vec<Sched> = c
            .prepare_cached(&format!(
                "SELECT flight, source, org, dst, dep_min, arr_min, type_code, n_flights, {wd} FROM ref_schedule \
                 WHERE callsign IN (?1, ?2) OR flight = ?3 ORDER BY rowid",
            ))?
            .query_map([names[0], names[1], number], |r| {
                Ok(Sched {
                    flight: r.get(0)?,
                    source: r.get(1)?,
                    org: r.get(2)?,
                    dst: r.get(3)?,
                    dep_min: r.get(4)?,
                    arr_min: r.get(5)?,
                    type_code: r.get(6)?,
                    n_flights: r.get(7)?,
                    weekdays: r.get(8)?,
                })
            })?
            .collect::<rusqlite::Result<_>>()?;
        crate::pgsort::sort(&mut rows, &|a: &Sched, b: &Sched| by_flights_desc(&a.n_flights, &b.n_flights));
        scheduled = rows;
    }
    let log = app.legs.with_conn(|c| crate::legs::flight(c, &callsign)).flatten();
    Ok((callsign, marketed, scheduled, log))
}

fn leg_row(org: &Option<String>, dst: &Option<String>) -> Value {
    let mut m = serde_json::Map::new();
    m.insert("org".into(), opt(org));
    m.insert("dst".into(), opt(dst));
    m.insert("flights".into(), 0.into());
    m.insert("days".into(), 0.into());
    m.insert("last".into(), Value::Null);
    Value::Object(m)
}

/// GET /v1/flights/{callsign}
pub async fn flight(State(app): State<Arc<App>>, axum::extract::Path(asked): axum::extract::Path<String>, req: Request) -> Response {
    if !catalog_here(&app) {
        return crate::proxy::forward(State(app), req).await.into_response();
    }
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "flight", app.settings.flight_rate_limit) {
        return e.into_response();
    }
    let asked = asked.trim().to_uppercase();
    if !(2..=12).contains(&asked.chars().count()) || !alnum(&asked) {
        return ApiError::new(422, "invalid_request", "invalid flight").into_response();
    }
    let a = app.clone();
    let facts = tokio::task::spawn_blocking(move || flight_facts(&a, &asked)).await;
    let Ok(Ok((callsign, marketed, scheduled, log))) = facts else {
        return ApiError::new(500, "internal_error", "internal error").into_response();
    };
    let routes_up = app.routes.available();
    let legs_up = app.legs.available();
    if !routes_up && !legs_up && scheduled.is_empty() {
        return dark().into_response();
    }
    let mut route = if routes_up { app.routes.get(&callsign) } else { None };
    let truthy = |v: &Value| !matches!(v, Value::Null | Value::Bool(false)) && v.as_array().is_none_or(|a| !a.is_empty());
    let mut route_source: Option<&str> = route.as_ref().filter(|r| truthy(r)).map(|_| "observed");
    if route.is_none() {
        match catalog_routes(&app, std::slice::from_ref(&callsign)).await {
            Ok(mut cat) => {
                if let Some(r) = cat.remove(&callsign) {
                    let known = app.gaps.get(&callsign).and_then(|q| q.get("known").cloned());
                    let answered = known.is_some_and(|k| r.as_array().is_some_and(|a| a.contains(&k)));
                    route_source = Some(if answered { "observed+catalog" } else { "catalog" });
                    route = Some(r);
                }
            }
            Err(e) => {
                eprintln!("flights: catalog: {e}");
                return ApiError::new(500, "internal_error", "internal error").into_response();
            }
        }
    }
    let log = if legs_up { log } else { None };
    let mut published = false;
    if route.is_none() && log.is_none() && !scheduled.is_empty() {
        // a service a board publishes that the network has not watched fly
        published = true;
        route = Some(Value::Array(vec![opt(&scheduled[0].org), opt(&scheduled[0].dst)]));
        route_source = Some("published");
    }
    if route.is_none() && log.is_none() {
        return ApiError::new(404, "not_observed", "no observations").into_response();
    }

    let mut legs: Option<Vec<Value>> = None;
    let (mut aircraft, mut recent) = (Value::Null, Value::Null);
    let mut coverage = "observed";
    if let Some(mut log) = log {
        let mut l: Vec<Value> = log["legs"].as_array().cloned().unwrap_or_default();
        aircraft = log["aircraft"].take();
        recent = log["recent"].take();
        let key = |v: &Value| (v["org"].clone(), v["dst"].clone());
        let mut seen: Vec<(Value, Value)> = l.iter().map(key).collect();
        for r in &scheduled {
            let k = (opt(&r.org), opt(&r.dst));
            if r.source.as_deref() == Some("published") && !seen.contains(&k) {
                l.push(leg_row(&r.org, &r.dst));
                seen.push(k);
            }
        }
        legs = Some(l);
    } else if published {
        legs = Some(scheduled.iter().map(|r| leg_row(&r.org, &r.dst)).collect());
        coverage = "published";
    } else if legs_up {
        legs = Some(vec![]);
        aircraft = Value::Array(vec![]);
        recent = Value::Array(vec![]);
    }
    if let Some(l) = legs.as_mut().filter(|l| !l.is_empty()) {
        // the callsign's own row first, then a board's (a stable sort)
        let mut own: Vec<&Sched> = scheduled.iter().collect();
        own.sort_by_key(|r| r.source.as_deref() == Some("published"));
        let mut sched: HashMap<(Value, Value), &Sched> = HashMap::new();
        let mut keys: Vec<(Value, Value)> = vec![];
        for r in own {
            let k = (opt(&r.org), opt(&r.dst));
            if !keys.contains(&k) {
                keys.push(k.clone());
                sched.insert(k, r);
            }
        }
        for leg in l.iter_mut() {
            let row = sched.get(&(leg["org"].clone(), leg["dst"].clone()));
            let m = leg.as_object_mut().unwrap();
            m.insert("dep".into(), row.map_or(Value::Null, |r| hhmm(r.dep_min)));
            m.insert("arr".into(), row.map_or(Value::Null, |r| hhmm(r.arr_min)));
            m.insert("type".into(), row.map_or(Value::Null, |r| opt(&r.type_code)));
            m.insert("times".into(), row.map_or(Value::Null, |r| opt(&r.source)));
            m.insert("flight".into(), row.map_or(Value::Null, |r| opt(&r.flight)));
            // the weekdays it keeps another slot, only when there are any
            if let Some(days) = row.and_then(|r| crate::boards::weekday_times(r.weekdays.as_deref())) {
                m.insert("weekdays".into(), days);
            }
        }
    }
    let mut m = serde_json::Map::new();
    m.insert("callsign".into(), callsign.into());
    m.insert("marketed".into(), marketed.map_or(Value::Null, Value::String));
    m.insert("route".into(), route.unwrap_or(Value::Null));
    m.insert("route_source".into(), route_source.map_or(Value::Null, |s| s.into()));
    m.insert("legs".into(), legs.map_or(Value::Null, Value::Array));
    m.insert("aircraft".into(), aircraft);
    m.insert("recent".into(), recent);
    m.insert("window_days".into(), if legs_up { app.legs.window_days().map_or(Value::Null, Value::from) } else { Value::Null });
    m.insert("coverage".into(), coverage.into());
    let mut out = String::with_capacity(2048);
    write_value(&mut out, &Value::Object(m));
    json(out, CACHE)
}
