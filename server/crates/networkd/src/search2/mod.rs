//! `/v2/search`: one box over flights, airlines, airports, airframes and
//! aircraft types, read before it is searched (a flight number, a route
//! in words, an airline and a place or a family, a city, a type, a
//! registration), answered from a full-text index built nightly from the
//! reference snapshot (`networkd search-index build`, build.rs) and
//! opened read-only. Results are typed: each kind has its own fields.
//!
//! The index directory holds generations and a CURRENT file naming the
//! one to serve; it is re-read every 30 s, and a new generation is
//! opened and swapped in whole. No index: 503, never another answer.
//! /v1/search is untouched (search.rs).

pub mod build;
mod engine;
mod intent;
mod lexicon;
mod schema;
mod text;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::extract::{Request, State};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use serde_json::{json, Value};

pub use engine::Engine;

use crate::http::{client_ip, json as respond, peer_of, throttle, ApiError};
use crate::search::{norm, quote, CACHE};
use crate::state::App;
use schema::KINDS;

const RECHECK: Duration = Duration::from_secs(30);
const CACHE_MAX: usize = 20_000;
const LIMIT: usize = 10;
const LIMIT_MAX: usize = 50;
/// a caller's position is rounded to this many degrees (~25 km)
const NEAR_STEP: f64 = 0.25;

struct Held {
    next_check: Option<Instant>,
    name: Option<String>,
    engine: Option<Arc<Engine>>,
    cache: HashMap<String, Bytes>,
}

/// The index directory and the generation open from it.
pub struct SearchIndex {
    dir: PathBuf,
    held: Mutex<Held>,
}

impl SearchIndex {
    pub fn new(dir: &str) -> Arc<SearchIndex> {
        Arc::new(SearchIndex {
            dir: dir.into(),
            held: Mutex::new(Held { next_check: None, name: None, engine: None, cache: HashMap::new() }),
        })
    }

    /// The current generation, opened when CURRENT names a new one.
    /// Blocking (it may open an index): call off the async threads.
    pub fn engine(&self) -> Option<Arc<Engine>> {
        let mut h = self.held.lock().unwrap();
        let now = Instant::now();
        if h.next_check.is_some_and(|t| now < t) {
            return h.engine.clone();
        }
        h.next_check = Some(now + RECHECK);
        let name = std::fs::read_to_string(self.dir.join("CURRENT")).ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
        let Some(name) = name else {
            if h.engine.is_some() {
                eprintln!("search index: {} has no CURRENT; /v2/search answers 503", self.dir.display());
            }
            h.engine = None;
            h.name = None;
            h.cache.clear();
            return None;
        };
        if h.name.as_ref() != Some(&name) || h.engine.is_none() {
            match Engine::open(&self.dir.join(&name)) {
                Ok(e) => {
                    eprintln!("search index: {} (generation {name})", self.dir.display());
                    h.engine = Some(Arc::new(e));
                    h.name = Some(name);
                    h.cache.clear();
                }
                // keep serving the generation already open, if any
                Err(e) => eprintln!("search index: {name}: {e}"),
            }
        }
        h.engine.clone()
    }

    fn cached(&self, key: &str) -> Option<Bytes> {
        self.held.lock().unwrap().cache.get(key).cloned()
    }

    fn remember(&self, key: String, generation: &str, body: Bytes) {
        let mut h = self.held.lock().unwrap();
        if h.engine.as_ref().is_none_or(|e| e.generation != generation) {
            return;
        }
        if h.cache.len() >= CACHE_MAX {
            h.cache.clear();
        }
        h.cache.insert(key, body);
    }
}

/// The request's parameters, each in its canonical spelling.
#[derive(Debug, PartialEq)]
struct Params {
    q: String,
    kinds: Vec<&'static str>,
    limit: usize,
    near: Option<(f64, f64)>,
}

fn invalid(detail: &str) -> Response {
    ApiError::new(422, "invalid_request", detail).into_response()
}

fn canonical_kinds(kinds: &[&'static str]) -> String {
    kinds.join(",")
}

fn canonical_near(near: (f64, f64)) -> String {
    format!("{},{}", near.0, near.1)
}

fn round_near(x: f64) -> f64 {
    let r = (x / NEAR_STEP).round() * NEAR_STEP;
    if r == 0.0 {
        0.0
    } else {
        r
    }
}

/// Validate the query string: (params, whether every one was spelled
/// canonically) or the 422.
fn params(query: Option<&str>) -> Result<(Params, bool), Response> {
    let mut last: HashMap<String, String> = HashMap::new();
    let mut repeated = false;
    for (k, v) in form_urlencoded::parse(query.unwrap_or("").as_bytes()) {
        if matches!(k.as_ref(), "q" | "kinds" | "limit" | "near") {
            repeated |= last.insert(k.into_owned(), v.into_owned()).is_some();
        }
    }
    let Some(raw) = last.get("q") else { return Err(invalid("query.q: Field required")) };
    let n = raw.chars().count();
    if n < 1 {
        return Err(invalid("query.q: String should have at least 1 character"));
    }
    if n > 40 {
        return Err(invalid("query.q: String should have at most 40 characters"));
    }
    let q = norm(raw);
    let mut same = !repeated && *raw == q;
    let mut kinds: Vec<&'static str> = KINDS.to_vec();
    if let Some(k) = last.get("kinds") {
        let asked: Vec<&str> = k.split(',').map(str::trim).filter(|s| !s.is_empty()).collect();
        if asked.is_empty() || asked.iter().any(|a| !KINDS.contains(a)) {
            return Err(invalid("query.kinds: a comma-separated list of flight, airline, airport, aircraft, type"));
        }
        kinds = KINDS.iter().copied().filter(|x| asked.contains(x)).collect();
        same &= *k == canonical_kinds(&kinds);
    }
    let mut limit = LIMIT;
    if let Some(l) = last.get("limit") {
        match l.parse::<usize>() {
            Ok(v) if (1..=LIMIT_MAX).contains(&v) => {
                limit = v;
                same &= *l == v.to_string();
            }
            _ => return Err(invalid("query.limit: an integer from 1 to 50")),
        }
    }
    let mut near = None;
    if let Some(s) = last.get("near") {
        let parsed = s.split_once(',').and_then(|(a, b)| Some((a.trim().parse::<f64>().ok()?, b.trim().parse::<f64>().ok()?)));
        match parsed {
            Some((lat, lon)) if lat.is_finite() && lon.is_finite() && lat.abs() <= 90.0 && lon.abs() <= 180.0 => {
                let r = (round_near(lat), round_near(lon));
                near = Some(r);
                same &= *s == canonical_near(r);
            }
            _ => return Err(invalid("query.near: lat,lon in degrees")),
        }
    }
    Ok((Params { q, kinds, limit, near }, same))
}

/// This request with its parameters in canonical spelling, any other
/// parameter as it came: 301, cached like the answer.
fn redirect(uri: &axum::http::Uri, p: &Params) -> Response {
    let mut canon: Vec<(&str, String)> = vec![("q", p.q.clone())];
    let query = uri.query().unwrap_or("");
    let present = |name: &str| form_urlencoded::parse(query.as_bytes()).any(|(k, _)| k == name);
    if present("kinds") {
        canon.push(("kinds", canonical_kinds(&p.kinds)));
    }
    if present("limit") {
        canon.push(("limit", p.limit.to_string()));
    }
    if let Some(n) = p.near {
        canon.push(("near", canonical_near(n)));
    }
    let mut parts: Vec<String> = vec![];
    let mut placed: Vec<&str> = vec![];
    for piece in query.split('&').filter(|x| !x.is_empty()) {
        let key = piece.split('=').next().unwrap_or("");
        let key = form_urlencoded::parse(key.as_bytes()).next().map(|(k, _)| k.into_owned()).unwrap_or_default();
        if let Some((name, value)) = canon.iter().find(|(n, _)| *n == key) {
            if !placed.contains(name) {
                parts.push(format!("{name}={}", quote(value).replace("%2C", ",")));
                placed.push(name);
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

fn unavailable() -> Response {
    ApiError::new(503, "artifact_unavailable", "search index not loaded").header("Retry-After", "60").into_response()
}

/// The answer's body.
fn answer(e: &Engine, p: &Params, site: &str) -> tantivy::Result<String> {
    let ask = engine::Ask { q: &p.q, kinds: &p.kinds, limit: p.limit, near: p.near };
    let found = e.search(&ask)?;
    let results: Vec<Value> = found.results.into_iter().map(|(s, d)| engine::served(s, d, site)).collect();
    let body = json!({"q": p.q, "intent": found.intent, "as_of": e.as_of, "index": e.generation, "results": results});
    Ok(body.to_string())
}

/// GET /v2/search
pub async fn search(State(app): State<Arc<App>>, req: Request) -> Response {
    let (p, same) = match params(req.uri().query()) {
        Ok(x) => x,
        Err(r) => return r,
    };
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "search_v2", app.settings.search_v2_rate_limit) {
        return e.into_response();
    }
    if p.q.chars().count() < 2 {
        return invalid("type at least two characters");
    }
    if !same {
        return redirect(req.uri(), &p);
    }
    let idx = app.search_index.clone();
    let site = app.settings.site_url.clone();
    let done = tokio::task::spawn_blocking(move || -> Result<Option<Bytes>, tantivy::TantivyError> {
        let Some(e) = idx.engine() else { return Ok(None) };
        let key = format!("{}|{}|{}|{}|{:?}", e.generation, p.q, canonical_kinds(&p.kinds), p.limit, p.near);
        if let Some(body) = idx.cached(&key) {
            return Ok(Some(body));
        }
        let body = Bytes::from(answer(&e, &p, &site)?);
        idx.remember(key, &e.generation, body.clone());
        Ok(Some(body))
    })
    .await;
    match done {
        Ok(Ok(Some(body))) => respond(body, CACHE),
        Ok(Ok(None)) => unavailable(),
        Ok(Err(e)) => {
            eprintln!("search v2: {e}");
            ApiError::new(500, "internal_error", "internal error").into_response()
        }
        Err(e) => {
            eprintln!("search v2: {e}");
            ApiError::new(500, "internal_error", "internal error").into_response()
        }
    }
}

#[cfg(test)]
mod tests;
