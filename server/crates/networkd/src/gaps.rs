//! Open questions and who answered them (the Python service's /v1/gaps,
//! /v1/gaps/{callsign} and /v1/contributors, in contributions.py).
//!
//! The questions come from the nightly gaps artifact; the answers (the
//! catalog, claims, endorsements) are filed every ten minutes by the
//! contributions pull, so they are read from Postgres. The suggested
//! airports follow the Python scorer operation for operation.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use axum::extract::{Path, Request, State};
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, NaiveDate, Utc};
use serde_json::{Map, Value};

use crate::catalog::catalog_routes;
use crate::http::{client_ip, json, peer_of, query_param, throttle, ApiError};
use crate::pyjson::write_value;
use crate::range_km::RANGE_KM;
use crate::routebook::Network;
use crate::state::App;

const CACHE: &str = "public, s-maxage=600";
const CORRIDOR_DEG: f64 = 45.0;
const ROTATION_TOLERANCE: f64 = 0.25;
const ROTATION_FLOOR_KM: f64 = 400.0;
const MIN_ROTATIONS: f64 = 2.0;
const SUGGESTIONS: usize = 3;

fn dark() -> ApiError {
    ApiError::new(503, "artifact_unavailable", "the gaps artifact is not loaded").header("Retry-After", "300")
}

fn internal(e: impl std::fmt::Display) -> Response {
    eprintln!("gaps: {e}");
    ApiError::new(500, "internal_error", "internal error").into_response()
}

// ---- the scorer's geometry, as contributions.py computes it ----------------

fn py_mod(a: f64, b: f64) -> f64 {
    let m = a % b;
    if m != 0.0 && ((b < 0.0) != (m < 0.0)) {
        m + b
    } else if m == 0.0 {
        0.0f64.copysign(b)
    } else {
        m
    }
}

fn bearing(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let (la1, la2) = (lat1.to_radians(), lat2.to_radians());
    let dl = (lon2 - lon1).to_radians();
    let x = dl.sin() * la2.cos();
    let y = la1.cos() * la2.sin() - la1.sin() * la2.cos() * dl.cos();
    py_mod(x.atan2(y).to_degrees(), 360.0)
}

fn angle_between(a: f64, b: f64) -> f64 {
    (py_mod(a - b + 180.0, 360.0) - 180.0).abs()
}

fn distance_km(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let (la1, la2) = (lat1.to_radians(), lat2.to_radians());
    let dl = (lon2 - lon1).to_radians();
    let s1 = ((la2 - la1) / 2.0).sin();
    let s2 = (dl / 2.0).sin();
    let h = s1 * s1 + la1.cos() * la2.cos() * (s2 * s2);
    let r = h.sqrt();
    2.0 * 6371.0 * (if 1.0 < r { 1.0 } else { r }).asin()
}

/// A JSON number as Python sees it, or None (null, absent, not a number).
fn num(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::Bool(b)) => Some(*b as i64 as f64),
        _ => None,
    }
}

/// Python truthiness of a JSON value.
fn truthy(v: Option<&Value>) -> bool {
    match v {
        None | Some(Value::Null) | Some(Value::Bool(false)) => false,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
        Some(Value::Bool(true)) => true,
    }
}

/// Commercial airports with coordinates, (iata or ident, lat, lon), in
/// the table's order; and one airport's coordinates by code.
struct Airports {
    rows: Vec<(String, f64, f64)>,
}

fn airports(app: &App) -> rusqlite::Result<Arc<Airports>> {
    let c = app.refdb.conn()?;
    app.refdb.memo("gap_airports", &c, |c| {
        let mut stmt = c.prepare(
            "SELECT iata, ident, lat, lon FROM ref_airports WHERE role = 'commercial' AND lat IS NOT NULL ORDER BY rowid",
        )?;
        let rows = stmt
            .query_map([], |r| {
                let iata: Option<String> = r.get(0)?;
                let ident: String = r.get(1)?;
                Ok((iata.filter(|s| !s.is_empty()).unwrap_or(ident), r.get(2)?, r.get(3)?))
            })?
            .collect::<rusqlite::Result<_>>()?;
        Ok(Airports { rows })
    })
}

/// `_airport`: the airport a code names, IATA first (an airport with an
/// IATA code wins a tie with one known only by ident).
fn airport(app: &App, code: &str) -> rusqlite::Result<Option<(Option<f64>, Option<f64>)>> {
    let code = code.trim().to_uppercase();
    if code.is_empty() {
        return Ok(None);
    }
    let c = app.refdb.conn()?;
    let mut rows: Vec<(bool, Option<f64>, Option<f64>)> = c
        .prepare_cached("SELECT iata IS NULL, lat, lon FROM ref_airports WHERE iata = ?1 OR ident = ?1 ORDER BY rowid")?
        .query_map([&code], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?
        .collect::<rusqlite::Result<_>>()?;
    crate::pgsort::sort(&mut rows, &|a: &(bool, Option<f64>, Option<f64>), b: &(bool, Option<f64>, Option<f64>)| a.0.cmp(&b.0));
    Ok(rows.into_iter().next().map(|r| (r.1, r.2)))
}

/// `candidates`: airports the evidence allows for the missing end, best
/// first: inside the ring the rotation implies, within the type's range,
/// along the last heard track, never the known end or a stop already in
/// the chain; ranked by how much the airline flies there.
fn candidates(app: &App, index: &Airports, network: &Network, gap: &Map<String, Value>, callsign: &str) -> rusqlite::Result<Vec<String>> {
    let est_v = gap.get("est_km");
    if !truthy(est_v) || num(gap.get("n_rot")).unwrap_or(0.0) < MIN_ROTATIONS {
        return Ok(vec![]);
    }
    let Some(est) = num(est_v) else { return Ok(vec![]) };
    let known_code = gap.get("known").and_then(|k| k.as_str()).unwrap_or("");
    let Some((Some(klat), klon)) = airport(app, known_code)? else { return Ok(vec![]) };
    let klon = klon.unwrap_or(f64::NAN);
    let tolerance = if ROTATION_TOLERANCE * est > ROTATION_FLOOR_KM { ROTATION_TOLERANCE * est } else { ROTATION_FLOOR_KM };
    let ty = gap.get("type").and_then(|t| t.as_str()).unwrap_or("");
    let reach = RANGE_KM.iter().find(|(k, _)| *k == ty).map(|(_, v)| *v as f64);
    let (lat, lon, trk) = (num(gap.get("last_lat")), num(gap.get("last_lon")), num(gap.get("last_trk")));
    let heading = gap.get("side").and_then(|s| s.as_str()) == Some("dest") && lat.is_some() && lon.is_some() && trk.is_some();
    let prefix: String = callsign.chars().take(3).collect();
    let empty = HashMap::new();
    let served = network.get(&prefix).unwrap_or(&empty);
    let mut exclude: HashSet<&str> = HashSet::new();
    exclude.insert(known_code);
    for c in gap.get("chain").and_then(|c| c.as_array()).into_iter().flatten() {
        if let Some(s) = c.as_str() {
            exclude.insert(s);
        }
    }
    let mut out: Vec<(i64, f64, &str)> = vec![];
    for (code, alat, alon) in &index.rows {
        if exclude.contains(code.as_str()) {
            continue;
        }
        if (alat - klat).abs() * 111.0 > est + tolerance {
            continue;
        }
        let d = distance_km(klat, klon, *alat, *alon);
        if (d - est).abs() > tolerance || reach.is_some_and(|r| r != 0.0 && d > r) {
            continue;
        }
        if heading
            && angle_between(bearing(lat.unwrap(), lon.unwrap(), *alat, *alon), trk.unwrap()) > CORRIDOR_DEG
        {
            continue;
        }
        out.push((-served.get(code).copied().unwrap_or(0), (d - est).abs(), code));
    }
    out.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.total_cmp(&b.1)).then(a.2.cmp(b.2)));
    Ok(out.into_iter().take(SUGGESTIONS).map(|x| x.2.to_string()).collect())
}

fn gap_row(callsign: &str, gap: &Map<String, Value>) -> Map<String, Value> {
    let g = |k: &str| gap.get(k).cloned().unwrap_or(Value::Null);
    let mut m = Map::new();
    m.insert("callsign".into(), callsign.into());
    for k in ["side", "known", "hint", "chain", "type", "n_recent", "last_seen"] {
        m.insert(k.into(), g(k));
    }
    let heard = if gap.get("last_lat").is_some_and(|v| !v.is_null()) {
        let mut h = Map::new();
        h.insert("lat".into(), g("last_lat"));
        h.insert("lon".into(), g("last_lon"));
        h.insert("track".into(), g("last_trk"));
        Value::Object(h)
    } else {
        Value::Null
    };
    m.insert("last_heard".into(), heard);
    let rot = if num(gap.get("n_rot")).unwrap_or(0.0) >= MIN_ROTATIONS { g("est_km") } else { Value::Null };
    m.insert("rotation_km".into(), rot);
    m
}

/// A query string as pydantic reads an int: trimmed, a sign, digits with
/// single underscores between them, and a fraction only if all zeros.
pub(crate) fn pydantic_int(raw: &str) -> Option<i64> {
    let s = raw.trim();
    let s = match s.split_once('.') {
        Some((whole, frac)) if !frac.is_empty() && frac.bytes().all(|b| b == b'0') => whole,
        Some(_) => return None,
        None => s,
    };
    let (neg, digits) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    let b = digits.as_bytes();
    if b.is_empty() || !b[0].is_ascii_digit() || !b[b.len() - 1].is_ascii_digit() || digits.contains("__") {
        return None;
    }
    if !b.iter().all(|c| c.is_ascii_digit() || *c == b'_') {
        return None;
    }
    let v: i64 = digits.replace('_', "").parse().ok()?;
    Some(if neg { -v } else { v })
}

/// Pydantic's messages for the query checks FastAPI runs before the route.
fn int_param(q: Option<&str>, name: &str, default: i64, min: i64, max: Option<i64>) -> Result<i64, ApiError> {
    let bad = |msg: String| ApiError::new(422, "invalid_request", format!("query.{name}: {msg}"));
    let Some(raw) = query_param(q, name) else { return Ok(default) };
    let v = pydantic_int(&raw)
        .ok_or_else(|| bad("Input should be a valid integer, unable to parse string as an integer".into()))?;
    if v < min {
        return Err(bad(format!("Input should be greater than or equal to {min}")));
    }
    if let Some(max) = max.filter(|m| v > *m) {
        return Err(bad(format!("Input should be less than or equal to {max}")));
    }
    Ok(v)
}

/// GET /v1/gaps
pub async fn gaps(State(app): State<Arc<App>>, req: Request) -> Response {
    let Some(db) = &app.db else {
        return crate::proxy::forward(State(app), req).await.into_response();
    };
    let q = req.uri().query();
    let airline = query_param(q, "airline");
    if let Some(a) = &airline {
        let n = a.chars().count();
        if n < 2 {
            return ApiError::new(422, "invalid_request", "query.airline: String should have at least 2 characters").into_response();
        }
        if n > 3 {
            return ApiError::new(422, "invalid_request", "query.airline: String should have at most 3 characters").into_response();
        }
    }
    let side = query_param(q, "side");
    if side.as_deref().is_some_and(|s| s != "origin" && s != "dest") {
        return ApiError::new(422, "invalid_request", "query.side: String should match pattern '^(origin|dest)$'").into_response();
    }
    let limit = match int_param(q, "limit", 50, 1, Some(200)) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    let offset = match int_param(q, "offset", 0, 0, None) {
        Ok(v) => v,
        Err(e) => return e.into_response(),
    };
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "gaps", app.settings.gaps_rate_limit) {
        return e.into_response();
    }
    if !app.gaps.available() {
        return dark().into_response();
    }
    let answered: HashSet<String> = {
        let g = match db.get().await {
            Ok(g) => g,
            Err(e) => return internal(e),
        };
        match g.as_ref().unwrap().query("SELECT callsign FROM route_catalog WHERE valid_to IS NULL", &[]).await {
            Ok(rows) => rows.iter().map(|r| r.get(0)).collect(),
            Err(e) => return internal(e),
        }
    };
    let book = app.gaps.all();
    let prefix = airline.map(|a| a.to_uppercase()).filter(|a| !a.is_empty());
    let rows: Vec<&String> = book
        .ordered
        .iter()
        .filter(|cs| prefix.as_ref().is_none_or(|p| cs.starts_with(p.as_str())))
        .filter(|cs| side.as_ref().is_none_or(|s| book.by_callsign[*cs].get("side").and_then(|v| v.as_str()) == Some(s)))
        .filter(|cs| !answered.contains(*cs))
        .collect();
    let total = rows.len();
    let page: Vec<String> = rows.into_iter().skip(offset as usize).take(limit as usize).cloned().collect();
    let (a, b) = (app.clone(), book.clone());
    let built = tokio::task::spawn_blocking(move || -> rusqlite::Result<Vec<Value>> {
        let index = airports(&a)?;
        let network = a.routes.by_airline();
        let mut out = vec![];
        for cs in &page {
            let g = &b.by_callsign[cs];
            let mut row = gap_row(cs, g);
            let s = candidates(&a, &index, &network, g, cs)?;
            row.insert("suggested".into(), Value::Array(s.into_iter().map(Value::String).collect()));
            out.push(Value::Object(row));
        }
        Ok(out)
    })
    .await;
    let Ok(Ok(list)) = built else { return internal("suggestions failed") };
    let mut m = Map::new();
    m.insert("total".into(), Value::from(total));
    m.insert("offset".into(), Value::from(offset));
    m.insert("gaps".into(), Value::Array(list));
    m.insert("coverage".into(), "observed".into());
    let mut out = String::with_capacity(16384);
    write_value(&mut out, &Value::Object(m));
    json(out, CACHE)
}

/// GET /v1/gaps/{callsign}
pub async fn gap(State(app): State<Arc<App>>, Path(callsign): Path<String>, req: Request) -> Response {
    let Some(db) = &app.db else {
        return crate::proxy::forward(State(app), req).await.into_response();
    };
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "gaps", app.settings.gaps_rate_limit) {
        return e.into_response();
    }
    let callsign = callsign.trim().to_uppercase();
    if !(2..=12).contains(&callsign.chars().count()) || !callsign.chars().all(char::is_alphanumeric) {
        return ApiError::new(422, "invalid_request", "invalid callsign").into_response();
    }
    if !app.gaps.available() {
        return dark().into_response();
    }
    let Some(found) = app.gaps.get(&callsign) else {
        return ApiError::new(404, "not_found", "no open question").into_response();
    };
    let (a, cs, g) = (app.clone(), callsign.clone(), found.clone());
    let suggested = tokio::task::spawn_blocking(move || -> rusqlite::Result<Vec<String>> {
        let index = airports(&a)?;
        candidates(&a, &index, &a.routes.by_airline(), &g, &cs)
    })
    .await;
    let Ok(Ok(suggested)) = suggested else { return internal("suggestions failed") };
    let mut row = gap_row(&callsign, &found);
    row.insert("suggested".into(), Value::Array(suggested.into_iter().map(Value::String).collect()));
    // the catalog answer in force, with the day it took effect
    let catalog = match catalog_routes(&app, std::slice::from_ref(&callsign)).await {
        Ok(mut c) => c.remove(&callsign),
        Err(e) => return internal(e),
    };
    let g = match db.get().await {
        Ok(g) => g,
        Err(e) => return internal(e),
    };
    let c = g.as_ref().unwrap();
    let catalog = match catalog {
        None => Value::Null,
        Some(route) => {
            let today = Utc::now().date_naive();
            let from = c
                .query_opt(
                    "SELECT valid_from FROM route_catalog WHERE callsign = $1 AND valid_from <= $2 \
                     AND (valid_to IS NULL OR valid_to > $2) ORDER BY valid_from DESC LIMIT 1",
                    &[&callsign, &today],
                )
                .await;
            let from: Option<NaiveDate> = match from {
                Ok(r) => r.map(|r| r.get(0)),
                Err(e) => return internal(e),
            };
            let mut m = Map::new();
            m.insert("route".into(), route);
            m.insert("valid_from".into(), from.map_or(Value::Null, |d| Value::String(d.format("%Y-%m-%d").to_string())));
            Value::Object(m)
        }
    };
    row.insert("catalog".into(), catalog);
    let answers = match c
        .query(
            "SELECT origin, dest, status, verdict FROM claims WHERE callsign = $1 AND status <> 'rejected' ORDER BY first_at",
            &[&callsign],
        )
        .await
    {
        Ok(rows) => rows,
        Err(e) => return internal(e),
    };
    row.insert(
        "answers".into(),
        Value::Array(
            answers
                .iter()
                .map(|r| {
                    let mut m = Map::new();
                    m.insert("origin".into(), Value::String(r.get(0)));
                    m.insert("dest".into(), Value::String(r.get(1)));
                    m.insert("status".into(), Value::String(r.get(2)));
                    m.insert("verdict".into(), r.get::<_, Option<String>>(3).map_or(Value::Null, Value::String));
                    Value::Object(m)
                })
                .collect(),
        ),
    );
    let mut out = String::with_capacity(2048);
    write_value(&mut out, &Value::Object(row));
    json(out, CACHE)
}

/// GET /v1/contributors
pub async fn contributors(State(app): State<Arc<App>>, req: Request) -> Response {
    let Some(db) = &app.db else {
        return crate::proxy::forward(State(app), req).await.into_response();
    };
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "gaps", app.settings.gaps_rate_limit) {
        return e.into_response();
    }
    let g = match db.get().await {
        Ok(g) => g,
        Err(e) => return internal(e),
    };
    let c = g.as_ref().unwrap();
    let rows = match c
        .query(
            "SELECT endorsements.handle, count(DISTINCT claims.id), max(claims.reviewed_at) \
             FROM endorsements JOIN claims ON claims.id = endorsements.claim_id \
             WHERE claims.status = 'approved' AND endorsements.handle IS NOT NULL \
             GROUP BY endorsements.handle \
             ORDER BY count(DISTINCT claims.id) DESC, max(claims.reviewed_at) LIMIT 200",
            &[],
        )
        .await
    {
        Ok(r) => r,
        Err(e) => return internal(e),
    };
    let total: i64 = match c.query_one("SELECT count(*) FROM claims WHERE status = 'approved'", &[]).await {
        Ok(r) => r.get(0),
        Err(e) => return internal(e),
    };
    let mut m = Map::new();
    m.insert("answers".into(), Value::from(total));
    m.insert(
        "contributors".into(),
        Value::Array(
            rows.iter()
                .map(|r| {
                    let mut o = Map::new();
                    o.insert("handle".into(), Value::String(r.get(0)));
                    o.insert("answers".into(), Value::from(r.get::<_, i64>(1)));
                    let latest: Option<DateTime<Utc>> = r.get(2);
                    o.insert(
                        "latest".into(),
                        latest.map_or(Value::Null, |t| Value::String(t.date_naive().format("%Y-%m-%d").to_string())),
                    );
                    Value::Object(o)
                })
                .collect(),
        ),
    );
    let mut out = String::with_capacity(8192);
    write_value(&mut out, &Value::Object(m));
    json(out, CACHE)
}

#[cfg(test)]
mod tests {
    #[test]
    fn ints_like_pydantic() {
        let ok = [("5.0", 5), ("5.00", 5), ("0.0", 0), (" 5 ", 5), ("+5", 5), ("-0", 0), ("5_000", 5000), ("05", 5), ("5\t", 5), ("1_0", 10), (" 5.0", 5)];
        for (s, v) in ok {
            assert_eq!(super::pydantic_int(s), Some(v), "{s:?}");
        }
        for s in ["5.", ".0", "5.01", "5e0", "٥", "_5", "5_", "5__0", "", "abc", "1.5"] {
            assert_eq!(super::pydantic_int(s), None, "{s:?}");
        }
    }
}
