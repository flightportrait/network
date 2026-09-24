//! Responses, the error envelope, client addresses and CORS, matching
//! the Python service: every non-200 is `{"error": code, "detail": text}`
//! with `Cache-Control: no-store`.

use std::net::SocketAddr;
use std::sync::Arc;

use axum::body::Body;
use axum::extract::{ConnectInfo, Request, State};
use axum::http::{header, HeaderMap, HeaderValue, Method, StatusCode};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use bytes::Bytes;

use crate::pyjson::Obj;
use crate::ratelimit::Limited;
use crate::state::App;

pub const CACHE_LIVE: &str = "public, max-age=5, s-maxage=10";
pub const CACHE_POINT: &str = "public, s-maxage=5";

pub fn json(body: impl Into<Bytes>, cache: &'static str) -> Response {
    let mut r = Response::new(Body::from(body.into()));
    let h = r.headers_mut();
    h.insert(header::CONTENT_TYPE, HeaderValue::from_static("application/json"));
    h.insert(header::CACHE_CONTROL, HeaderValue::from_static(cache));
    r
}

#[derive(Debug)]
pub struct ApiError {
    pub status: StatusCode,
    pub code: &'static str,
    pub detail: String,
    pub headers: Vec<(&'static str, String)>,
}

impl ApiError {
    pub fn new(status: u16, code: &'static str, detail: impl Into<String>) -> ApiError {
        ApiError {
            status: StatusCode::from_u16(status).unwrap(),
            code,
            detail: detail.into(),
            headers: vec![],
        }
    }

    pub fn header(mut self, k: &'static str, v: impl Into<String>) -> ApiError {
        self.headers.push((k, v.into()));
        self
    }

    pub fn stale() -> ApiError {
        ApiError::new(503, "stale_snapshot", "sky data unavailable").header("Retry-After", "5")
    }

    pub fn limited(l: Limited) -> ApiError {
        let retry = l.retry_s.to_string();
        ApiError::new(429, "rate_limited", "slow down")
            .header("Retry-After", retry.clone())
            .header("RateLimit-Limit", l.limit.to_string())
            .header("RateLimit-Remaining", "0")
            .header("RateLimit-Reset", retry)
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut s = String::new();
        let mut o = Obj::new(&mut s);
        o.str("error", self.code).str("detail", &self.detail);
        o.end();
        let mut r = Response::new(Body::from(s));
        *r.status_mut() = self.status;
        let h = r.headers_mut();
        h.insert(header::CONTENT_TYPE, HeaderValue::from_static("application/json"));
        for (k, v) in self.headers {
            if let Ok(v) = HeaderValue::from_str(&v) {
                h.insert(k, v);
            }
        }
        h.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
        r
    }
}

pub type ApiResult = Result<Response, ApiError>;

/// The caller's address: the trusted proxy's header when configured,
/// else the socket peer.
pub fn client_ip(app: &App, headers: &HeaderMap, peer: Option<SocketAddr>) -> String {
    let name = &app.settings.client_ip_header;
    if !name.is_empty() {
        if let Some(v) = headers.get(name.as_str()).and_then(|v| v.to_str().ok()) {
            if !v.is_empty() {
                return v.split(',').next().unwrap_or("").trim().to_string();
            }
        }
    }
    peer.map(|p| p.ip().to_string()).unwrap_or_else(|| "unknown".into())
}

pub fn peer_of(req: &Request) -> Option<SocketAddr> {
    req.extensions().get::<ConnectInfo<SocketAddr>>().map(|c| c.0)
}

pub fn throttle(app: &App, ip: &str, bucket: &'static str, limit: usize) -> Result<(), ApiError> {
    app.limiter.check(bucket, ip, limit, app.settings.rate_window_s).map_err(ApiError::limited)
}

pub async fn not_found() -> ApiError {
    ApiError::new(404, "not_found", "Not Found")
}

pub async fn method_not_allowed() -> ApiError {
    ApiError::new(405, "method_not_allowed", "Method Not Allowed").header("Allow", "GET")
}

// ---- CORS (Starlette 1.x CORSMiddleware: GET only, no credentials) -------

const ALLOW_HEADERS: &str = "Accept, Accept-Language, Content-Language, Content-Type";
const ALLOWED_REQUEST_HEADERS: [&str; 4] = ["accept", "accept-language", "content-language", "content-type"];
const PREFLIGHT_VARY: &str =
    "Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network";

/// Runs before routing, so preflights never reach a route.
pub async fn cors(State(app): State<Arc<App>>, req: Request, next: Next) -> Response {
    let origins = &app.settings.cors_origins;
    let all = origins.iter().any(|o| o == "*");
    let origin = req.headers().get(header::ORIGIN).and_then(|v| v.to_str().ok()).map(str::to_string);
    let allowed = |o: &str| all || origins.iter().any(|x| x == o);

    if let Some(origin) = origin.as_deref().filter(|_| {
        req.method() == Method::OPTIONS && req.headers().contains_key("access-control-request-method")
    }) {
        let hv = |name: &str| req.headers().get(name).and_then(|v| v.to_str().ok());
        let mut failures = vec![];
        if !allowed(origin) {
            failures.push("origin");
        }
        if hv("access-control-request-method") != Some("GET") {
            failures.push("method");
        }
        if let Some(asked) = hv("access-control-request-headers") {
            if asked.split(',').any(|h| !ALLOWED_REQUEST_HEADERS.contains(&h.to_lowercase().trim())) {
                failures.push("headers");
            }
        }
        if hv("access-control-request-private-network").is_some() {
            failures.push("private-network");
        }
        let (status, body) = if failures.is_empty() {
            (StatusCode::OK, "OK".to_string())
        } else {
            (StatusCode::BAD_REQUEST, format!("Disallowed CORS {}", failures.join(", ")))
        };
        let mut r = Response::new(Body::from(body));
        *r.status_mut() = status;
        let h = r.headers_mut();
        h.insert(header::VARY, HeaderValue::from_static(PREFLIGHT_VARY));
        if all {
            h.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, HeaderValue::from_static("*"));
        } else if allowed(origin) {
            if let Ok(v) = HeaderValue::from_str(origin) {
                h.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, v);
            }
        }
        h.insert(header::ACCESS_CONTROL_ALLOW_METHODS, HeaderValue::from_static("GET"));
        h.insert(header::ACCESS_CONTROL_MAX_AGE, HeaderValue::from_static("600"));
        h.insert(header::ACCESS_CONTROL_ALLOW_HEADERS, HeaderValue::from_static(ALLOW_HEADERS));
        h.insert(header::CONTENT_TYPE, HeaderValue::from_static("text/plain; charset=utf-8"));
        return r;
    }

    let mut r = next.run(req).await;
    if r.extensions().get::<crate::proxy::Forwarded>().is_some() {
        return r; // the fallback set its own
    }
    let h = r.headers_mut();
    if let Some(origin) = origin.as_deref() {
        if all {
            h.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, HeaderValue::from_static("*"));
        } else if allowed(origin) {
            if let Ok(v) = HeaderValue::from_str(origin) {
                h.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, v);
            }
        }
    }
    // Starlette appends Vary: Origin on every response, joined into one value
    let vary = h
        .get_all(header::VARY)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .chain(std::iter::once("Origin"))
        .collect::<Vec<_>>()
        .join(", ");
    if let Ok(v) = HeaderValue::from_str(&vary) {
        h.insert(header::VARY, v);
    }
    r
}

/// Query parameters, the last value winning like Starlette's.
pub fn query_param(req_query: Option<&str>, name: &str) -> Option<String> {
    let q = req_query?;
    let mut found = None;
    for (k, v) in form_urlencoded::parse(q.as_bytes()) {
        if k == name {
            found = Some(v.into_owned());
        }
    }
    found
}
