//! The OpenAPI document and the docs pages, as the Python service's
//! FastAPI app serves them: copies committed in static/ (written by
//! api/export_openapi.py --networkd, and held equal to the app by
//! api/tests/test_openapi_static.py), built into the binary.

use std::sync::OnceLock;

use axum::body::Body;
use axum::http::{header, HeaderValue};
use axum::response::Response;

use crate::pyjson::write_value;

const OPENAPI: &str = include_str!("../static/openapi.json");

fn page(body: &'static [u8], ctype: &'static str) -> Response {
    let mut r = Response::new(Body::from(body));
    r.headers_mut().insert(header::CONTENT_TYPE, HeaderValue::from_static(ctype));
    r
}

/// GET /openapi.json, compact as Starlette's JSONResponse writes it.
pub async fn openapi() -> Response {
    static COMPACT: OnceLock<String> = OnceLock::new();
    let body = COMPACT.get_or_init(|| {
        let v: serde_json::Value = serde_json::from_str(OPENAPI).expect("static/openapi.json");
        let mut s = String::with_capacity(OPENAPI.len());
        write_value(&mut s, &v);
        s
    });
    page(body.as_bytes(), "application/json")
}

pub async fn swagger() -> Response {
    page(include_bytes!("../static/docs.html"), "text/html; charset=utf-8")
}

pub async fn oauth2_redirect() -> Response {
    page(include_bytes!("../static/oauth2-redirect.html"), "text/html; charset=utf-8")
}

pub async fn redoc() -> Response {
    page(include_bytes!("../static/redoc.html"), "text/html; charset=utf-8")
}
