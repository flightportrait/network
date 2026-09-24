//! Routes networkd does not serve yet go, unchanged, to the service that
//! does (NETWORKD_FALLBACK, e.g. the Python API). One public address for
//! the whole API while routes move over one at a time; a route stops
//! being forwarded the day networkd answers it.

use std::time::Duration;

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{header, HeaderName, Response, StatusCode};
use futures_util::StreamExt;
use std::sync::Arc;

use crate::http::{not_found, ApiError};
use crate::state::App;

const MAX_BODY: usize = 1 << 20;

/// Marks a response that came from the fallback: its CORS headers are
/// the fallback's own.
#[derive(Clone, Copy)]
pub struct Forwarded;

fn hop_by_hop(name: &HeaderName) -> bool {
    matches!(
        name.as_str(),
        "connection" | "keep-alive" | "proxy-authenticate" | "proxy-authorization" | "te" | "trailer"
            | "transfer-encoding" | "upgrade" | "host" | "content-length"
    )
}

pub struct Fallback {
    base: String,
    client: reqwest::Client,
}

impl Fallback {
    pub fn new(base: &str) -> Option<Fallback> {
        if base.is_empty() {
            return None;
        }
        let client = reqwest::Client::builder()
            .no_gzip()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(60))
            .redirect(reqwest::redirect::Policy::none())
            .pool_max_idle_per_host(32)
            .build()
            .expect("http client");
        Some(Fallback { base: base.trim_end_matches('/').to_string(), client })
    }
}

pub async fn forward(State(app): State<Arc<App>>, req: Request) -> Response<Body> {
    use axum::response::IntoResponse;
    let Some(fb) = app.fallback.as_ref() else {
        return not_found().await.into_response();
    };
    let path = req.uri().path_and_query().map(|p| p.as_str()).unwrap_or("/");
    let url = format!("{}{}", fb.base, path);
    let method = reqwest::Method::from_bytes(req.method().as_str().as_bytes()).unwrap_or(reqwest::Method::GET);
    let mut out = fb.client.request(method, &url);
    for (k, v) in req.headers() {
        if !hop_by_hop(k) {
            out = out.header(k.as_str(), v.as_bytes());
        }
    }
    let body = match axum::body::to_bytes(req.into_body(), MAX_BODY).await {
        Ok(b) => b,
        Err(_) => return ApiError::new(413, "invalid_request", "request body too large").into_response(),
    };
    if !body.is_empty() {
        out = out.body(body);
    }
    let resp = match out.send().await {
        Ok(r) => r,
        Err(e) => {
            eprintln!("fallback {url}: {e}");
            return ApiError::new(502, "unavailable", "upstream unavailable").into_response();
        }
    };
    let status = StatusCode::from_u16(resp.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let headers = resp.headers().clone();
    let mut r = Response::new(Body::from_stream(resp.bytes_stream().map(|c| c.map_err(std::io::Error::other))));
    *r.status_mut() = status;
    for (k, v) in &headers {
        if let (Ok(name), Ok(value)) = (
            HeaderName::from_bytes(k.as_str().as_bytes()),
            header::HeaderValue::from_bytes(v.as_bytes()),
        ) {
            if !hop_by_hop(&name) {
                r.headers_mut().append(name, value);
            }
        }
    }
    r.extensions_mut().insert(Forwarded);
    r
}
