//! Live-sky routes: counts, the aircraft list, the point query, trails.
//! Everything reads the published snapshot and nothing else; throttle
//! first, cache headers explicit on every response.

use std::sync::Arc;
use std::time::Instant;

use axum::extract::{Path, Request, State};
use axum::http::header;
use axum::response::Response;

use crate::departure::{flight_bounds, Bounds};
use crate::http::{client_ip, json, peer_of, query_param, throttle, ApiError, ApiResult, CACHE_LIVE, CACHE_POINT};
use crate::pyjson::{round_to, write_float, Obj};
use crate::sky::{BBox, Snapshot};
use crate::state::{now_s, App};

const EARTH_RADIUS_NM: f64 = 3440.065;
const BOUNDS_TTL_S: f64 = 60.0;

pub fn fresh(app: &App) -> Result<Arc<Snapshot>, ApiError> {
    let snap = app.snapshot();
    if !snap.fresh(app.settings.stale_after_s) {
        // a dead upstream reads as an outage, never as an empty sky
        return Err(ApiError::stale());
    }
    Ok(snap)
}

/// Python's `float(x)`: surrounding whitespace allowed, `inf`/`nan` too.
pub fn py_float(s: &str) -> Option<f64> {
    let t = s.trim();
    if t.is_empty() {
        return None;
    }
    let t = t.replace('_', "");
    t.parse::<f64>().ok()
}

/// `minLon,minLat,maxLon,maxLat` -> box. A west edge east of the east
/// edge crosses the antimeridian.
pub fn parse_bbox(bbox: &str) -> Result<BBox, ApiError> {
    let parts: Vec<Option<f64>> = bbox.split(',').map(py_float).collect();
    let bad = || ApiError::new(422, "invalid_request", "bbox is minLon,minLat,maxLon,maxLat");
    if parts.len() != 4 || parts.iter().any(|p| p.is_none_or(|x| !x.is_finite())) {
        return Err(bad());
    }
    let (w, s, e, n) = (parts[0].unwrap(), parts[1].unwrap(), parts[2].unwrap(), parts[3].unwrap());
    if !(-90.0 <= s && s <= n && n <= 90.0) || !((-180.0..=180.0).contains(&w) && (-180.0..=180.0).contains(&e)) {
        return Err(ApiError::new(422, "invalid_request", "bbox out of range"));
    }
    Ok(BBox { w, s, e, n })
}

pub async fn now(State(app): State<Arc<App>>, req: Request) -> ApiResult {
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    throttle(&app, &ip, "now", app.settings.now_rate_limit)?;
    let snap = fresh(&app)?;
    let station_count = {
        let p = app.presence.lock().unwrap();
        let fresh_presence = now_s() - p.at <= app.settings.station_presence_stale_s as f64;
        (p.available && fresh_presence).then_some(p.count)
    };
    let mut s = String::with_capacity(160);
    let mut o = Obj::new(&mut s);
    o.int("aircraft_count", snap.count() as i64).int("aircraft_with_pos", snap.with_pos as i64);
    match station_count {
        Some(n) => o.int("station_count", n as i64),
        None => o.null("station_count"),
    };
    o.f64("generated_at", snap.generated_at);
    match app.legs.archive_through() {
        Some(d) => o.str("archive_through", &d),
        None => o.null("archive_through"),
    };
    o.end();
    Ok(json(s, CACHE_LIVE))
}

pub async fn aircraft(State(app): State<Arc<App>>, req: Request) -> ApiResult {
    let bbox = query_param(req.uri().query(), "bbox");
    if bbox.as_ref().is_some_and(|b| b.chars().count() > 80) {
        return Err(ApiError::new(422, "invalid_request", "query.bbox: String should have at most 80 characters"));
    }
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    throttle(&app, &ip, "aircraft", app.settings.aircraft_rate_limit)?;
    let bbox = match bbox.as_deref() {
        Some(b) if !b.is_empty() => Some(parse_bbox(b)?),
        _ => None,
    };
    let snap = fresh(&app)?;
    Ok(match bbox {
        None => json(snap.body(), CACHE_LIVE),
        Some(b) => json(snap.render(Some(&b)), CACHE_LIVE),
    })
}

pub fn distance_nm(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let (p1, p2) = (lat1.to_radians(), lat2.to_radians());
    let dp = p2 - p1;
    let dl = (lon2 - lon1).to_radians();
    let a = (dp / 2.0).sin().powi(2) + p1.cos() * p2.cos() * (dl / 2.0).sin().powi(2);
    2.0 * EARTH_RADIUS_NM * a.sqrt().asin()
}

pub async fn point(
    State(app): State<Arc<App>>,
    Path((lat, lon, radius)): Path<(String, String, String)>,
    req: Request,
) -> ApiResult {
    // path validation happens before the handler, as FastAPI's does
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
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    throttle(&app, &ip, "point", app.settings.point_rate_limit)?;
    if !((-90.0..=90.0).contains(&lat) && (-180.0..=180.0).contains(&lon)) {
        return Err(ApiError::new(422, "invalid_request", "invalid coordinates"));
    }
    if radius <= 0.0 {
        return Err(ApiError::new(422, "invalid_request", "invalid radius"));
    }
    let radius = radius.min(app.settings.max_point_radius_nm as f64);
    let started = Instant::now();
    let snap = fresh(&app)?;
    let mut hits: Vec<(f64, &str)> = vec![];
    for e in &snap.entries {
        let (Some(alat), Some(alon)) = (e.lat, e.lon) else { continue };
        let d = distance_nm(lat, lon, alat, alon);
        if d <= radius {
            hits.push((round_to(d, 1), &e.json));
        }
    }
    hits.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut s = String::with_capacity(hits.iter().map(|h| h.1.len() + 16).sum::<usize>() + 128);
    let mut o = Obj::new(&mut s);
    let buf = o.key("ac");
    buf.push('[');
    for (i, (d, j)) in hits.iter().enumerate() {
        if i > 0 {
            buf.push(',');
        }
        buf.push_str(&j[..j.len() - 1]);
        buf.push_str(if j.len() > 2 { ",\"dst\":" } else { "\"dst\":" });
        write_float(buf, *d);
        buf.push('}');
    }
    buf.push(']');
    o.str("msg", "No error")
        .f64("now", snap.generated_at)
        .int("total", hits.len() as i64)
        .f64("ctime", snap.generated_at)
        .f64("ptime", round_to(started.elapsed().as_secs_f64() * 1000.0, 3));
    o.end();
    Ok(json(s, CACHE_POINT))
}

async fn bounds_of(app: &App, hex: &str) -> Bounds {
    let now = now_s();
    if let Some((at, b)) = app.bounds_cache.lock().unwrap().get(hex) {
        if now - at < BOUNDS_TTL_S {
            return b.clone();
        }
    }
    let b = match app.upstream.trace(hex).await {
        Ok(body) => serde_json::from_slice(&body).map(|v| flight_bounds(&v)).unwrap_or_default(),
        Err(_) => Bounds::default(),
    };
    let mut cache = app.bounds_cache.lock().unwrap();
    if cache.len() > 5000 {
        cache.clear();
    }
    cache.insert(hex.to_string(), (now, b.clone()));
    b
}

pub async fn trace(State(app): State<Arc<App>>, Path(hex): Path<String>, req: Request) -> ApiResult {
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    throttle(&app, &ip, "trace", app.settings.trace_rate_limit)?;
    let Some(points) = app.traces.lock().unwrap().get_json(&hex) else {
        return Err(ApiError::new(404, "not_found", "no trace"));
    };
    let h = hex.trim().to_lowercase();
    let bounds = bounds_of(&app, &h).await;
    let mut s = String::with_capacity(points.len() + 160);
    let mut o = Obj::new(&mut s);
    o.str("hex", &h).raw("points", &points).raw("departure", &bounds.departure).raw("arrival", &bounds.arrival);
    o.end();
    Ok(json(s, CACHE_POINT))
}

pub async fn healthz() -> Response {
    let mut r = json("{\"ok\":true}", "no-store");
    r.headers_mut().remove(header::CACHE_CONTROL);
    r
}

pub async fn index(State(app): State<Arc<App>>) -> Response {
    let s = &app.settings;
    let mut out = String::new();
    let mut o = Obj::new(&mut out);
    o.str("name", "FlightPortrait network API")
        .str("docs", "https://docs.flightportrait.com/api/reference")
        .str("openapi", "/openapi.json")
        .str("swagger", "/docs")
        .str("source", &s.source_url)
        .str("terms", &s.terms_url)
        .str("attribution", &s.attribution)
        .str("credits", &s.credits_url)
        .str("feed", "feed.flightportrait.com:30004 (beast_reduce_plus_out)");
    o.end();
    let mut r = json(out, "no-store");
    r.headers_mut().remove(header::CACHE_CONTROL);
    r
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bbox_rules() {
        assert!(parse_bbox("1,2,3,4").is_ok());
        assert_eq!(parse_bbox("170,0,-170,20").unwrap().w, 170.0);
        assert_eq!(parse_bbox("1,2,3").err().unwrap().detail, "bbox is minLon,minLat,maxLon,maxLat");
        assert_eq!(parse_bbox("1,nan,3,4").err().unwrap().detail, "bbox is minLon,minLat,maxLon,maxLat");
        assert_eq!(parse_bbox("1,5,3,4").err().unwrap().detail, "bbox out of range");
        assert_eq!(parse_bbox(" 1 ,2,3,4").unwrap().w, 1.0);
    }

    #[test]
    fn haversine_matches_python() {
        // math in CPython: 2*3440.065*asin(sqrt(...)) for (1.3521,103.8198)->(1.5,104.0)
        let d = distance_nm(1.3521, 103.8198, 1.5, 104.0);
        assert_eq!(round_to(d, 1), 14.0);
    }
}
