//! Find my station (the Python service's setup_beacon.py): a Station
//! waiting for setup reports its address on the home network, and the
//! join page, opened on a phone on that same network, asks for it. Both
//! reach us from the same public address, and that is the pairing.
//!
//! Held in memory only, BEACON_TTL_S after the last report, under a
//! salted hash of the public address (IPv6 counts by its /64). Nothing is
//! written anywhere and nothing survives a restart. Only private IPv4
//! addresses are accepted, and a network is only answered with its own.

use std::collections::HashMap;
use std::net::{IpAddr, Ipv4Addr};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use indexmap::IndexMap;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::http::{client_ip, json, peer_of, throttle, ApiError};
use crate::pyjson::{write_str, Obj};
use crate::state::App;

const BEACON_TTL_S: f64 = 600.0;
const MAX_PER_NETWORK: usize = 8;
const MAX_BODY: usize = 64 * 1024;

pub struct Beacons {
    salt: [u8; 16],
    /// network -> {lan: (port, name, seen)}
    book: HashMap<String, IndexMap<String, (i64, String, Instant)>>,
}

impl Beacons {
    pub fn new() -> Mutex<Beacons> {
        // per process: memory only
        let mut salt = [0u8; 16];
        use std::io::Read;
        if std::fs::File::open("/dev/urandom").and_then(|mut f| f.read_exact(&mut salt)).is_err() {
            use std::hash::{BuildHasher, Hasher};
            for chunk in salt.chunks_mut(8) {
                let mut h = std::collections::hash_map::RandomState::new().build_hasher();
                h.write_u64(std::process::id() as u64);
                chunk.copy_from_slice(&h.finish().to_le_bytes()[..chunk.len()]);
            }
        }
        Mutex::new(Beacons { salt, book: HashMap::new() })
    }

    /// The key a home is known by: its IPv4 address, or its IPv6 /64.
    fn network_of(&self, ip: &str) -> String {
        let key = match ip.parse::<IpAddr>() {
            Ok(IpAddr::V6(v6)) => match v6.to_ipv4_mapped() {
                Some(v4) => v4.to_string(),
                None => {
                    let s = v6.segments();
                    format!("{:x}:{:x}:{:x}:{:x}::/64", s[0], s[1], s[2], s[3])
                }
            },
            Ok(IpAddr::V4(v4)) => v4.to_string(),
            Err(_) => ip.to_string(),
        };
        let mut h = Sha256::new();
        h.update(self.salt);
        h.update(key.as_bytes());
        h.finalize().iter().map(|b| format!("{b:02x}")).collect()
    }

    fn sweep(&mut self, now: Instant) {
        self.book.retain(|_, entries| {
            entries.retain(|_, v| now.duration_since(v.2).as_secs_f64() <= BEACON_TTL_S);
            !entries.is_empty()
        });
    }
}

/// Python 3.12's `is_private` for IPv4, less loopback, link-local and
/// unspecified: an address a phone on the same network can open.
fn lan_ok(lan: &str) -> bool {
    let Ok(a) = lan.parse::<Ipv4Addr>() else { return false };
    let n = u32::from(a);
    let within = |net: [u8; 4], bits: u32| {
        let base = u32::from(Ipv4Addr::from(net));
        let mask = if bits == 0 { 0 } else { u32::MAX << (32 - bits) };
        n & mask == base
    };
    const PRIVATE: [([u8; 4], u32); 14] = [
        ([0, 0, 0, 0], 8),
        ([10, 0, 0, 0], 8),
        ([127, 0, 0, 0], 8),
        ([169, 254, 0, 0], 16),
        ([172, 16, 0, 0], 12),
        ([192, 0, 0, 0], 24),
        ([192, 0, 0, 170], 31),
        ([192, 0, 2, 0], 24),
        ([192, 168, 0, 0], 16),
        ([198, 18, 0, 0], 15),
        ([198, 51, 100, 0], 24),
        ([203, 0, 113, 0], 24),
        ([240, 0, 0, 0], 4),
        ([255, 255, 255, 255], 32),
    ];
    let exception = within([192, 0, 0, 9], 32) || within([192, 0, 0, 10], 32);
    let private = !exception && PRIVATE.iter().any(|(net, bits)| within(*net, *bits));
    private && !a.is_loopback() && !a.is_link_local() && !a.is_unspecified()
}

/// `^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`
fn hostname(s: &str) -> bool {
    let b = s.as_bytes();
    let ok = |c: &u8| c.is_ascii_lowercase() || c.is_ascii_digit();
    !b.is_empty()
        && b.len() <= 63
        && ok(&b[0])
        && ok(&b[b.len() - 1])
        && b.iter().all(|c| ok(c) || *c == b'-')
}

fn invalid(detail: impl Into<String>) -> Response {
    ApiError::new(422, "invalid_request", detail).into_response()
}

/// A JSON value as pydantic's lax int reads it.
fn lax_int(v: &Value) -> Result<i128, &'static str> {
    const NOT_INT: &str = "Input should be a valid integer";
    match v {
        Value::Bool(b) => Ok(*b as i128),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                Ok(i as i128)
            } else if let Some(u) = n.as_u64() {
                Ok(u as i128)
            } else {
                let f = n.as_f64().unwrap_or(f64::NAN);
                if f.fract() == 0.0 && f.is_finite() {
                    Ok(f as i128)
                } else {
                    Err("Input should be a valid integer, got a number with a fractional part")
                }
            }
        }
        Value::String(s) => {
            crate::gaps::pydantic_int(s).map(i128::from).ok_or("Input should be a valid integer, unable to parse string as an integer")
        }
        _ => Err(NOT_INT),
    }
}

/// POST /v1/setup/beacon {lan, port=8654, name="station"}
pub async fn report(State(app): State<Arc<App>>, req: Request) -> Response {
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    // FastAPI reads the body as JSON only for a JSON content type (or none)
    let ctype = req.headers().get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).map(str::to_string);
    let json_body = ctype.as_deref().is_none_or(|t| {
        let t = t.split(';').next().unwrap_or("").trim().to_ascii_lowercase();
        t.split_once('/').is_some_and(|(main, sub)| main == "application" && (sub == "json" || sub.ends_with("+json")))
    });
    let Ok(bytes) = axum::body::to_bytes(req.into_body(), MAX_BODY).await else {
        return ApiError::new(413, "invalid_request", "request body too large").into_response();
    };
    if bytes.is_empty() {
        return invalid("body: Field required");
    }
    let not_object = "body: Input should be a valid dictionary or object to extract fields from";
    if !json_body {
        return invalid(not_object);
    }
    let body: Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(e) => {
            // Python reports where its decoder stopped: the end for a cut
            // body, else the first character it could not read
            let text = String::from_utf8_lossy(&bytes);
            let chars: Vec<char> = text.chars().collect();
            let mut at = if e.is_eof() {
                chars.len()
            } else {
                let (line, col) = (e.line(), e.column());
                let start: usize = text.split('\n').take(line.saturating_sub(1)).map(|l| l.chars().count() + 1).sum();
                (start + col.saturating_sub(1)).min(chars.len())
            };
            // serde stops inside or after a bad word (`not`, `tru`), Python
            // at its start
            while at > 0 && chars[at - 1].is_ascii_alphabetic() {
                at -= 1;
            }
            return invalid(format!("body.{at}: JSON decode error"));
        }
    };
    let Value::Object(o) = body else { return invalid(not_object) };
    let lan = match o.get("lan") {
        None => return invalid("body.lan: Field required"),
        Some(Value::String(s)) => s.clone(),
        Some(_) => return invalid("body.lan: Input should be a valid string"),
    };
    let port = match o.get("port") {
        None => 8654,
        Some(v) => match lax_int(v) {
            Ok(p) => p,
            Err(msg) => return invalid(format!("body.port: {msg}")),
        },
    };
    let name = match o.get("name") {
        None => "station".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(_) => return invalid("body.name: Input should be a valid string"),
    };
    if let Err(e) = throttle(&app, &ip, "beacon", 30) {
        return e.into_response();
    }
    let name = name.trim().to_lowercase();
    if !lan_ok(&lan) || !(1..=65535).contains(&port) || !hostname(&name) {
        return invalid("lan must be a private IPv4 address, port 1-65535, name a hostname");
    }
    let now = Instant::now();
    {
        let mut b = app.beacons.lock().unwrap();
        b.sweep(now);
        let net = b.network_of(&ip);
        let entries = b.book.entry(net).or_default();
        if !entries.contains_key(&lan) && entries.len() >= MAX_PER_NETWORK {
            let mut oldest: Option<(&String, Instant)> = None;
            for (k, v) in entries.iter() {
                if oldest.is_none_or(|o| v.2 < o.1) {
                    oldest = Some((k, v.2));
                }
            }
            if let Some(k) = oldest.map(|o| o.0.clone()) {
                entries.shift_remove(&k);
            }
        }
        entries.insert(lan, (port as i64, name, now));
    }
    let mut r = Response::new(Body::empty());
    *r.status_mut() = StatusCode::NO_CONTENT;
    r.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    r
}

/// Any other method: Starlette answers with the first route registered
/// for the path, the report.
pub async fn other_method() -> ApiError {
    ApiError::new(405, "method_not_allowed", "Method Not Allowed").header("Allow", "POST")
}

/// GET /v1/setup/beacon: the Stations waiting on the caller's network,
/// newest first.
pub async fn find(State(app): State<Arc<App>>, req: Request) -> Response {
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "beacon_find", 120) {
        return e.into_response();
    }
    let now = Instant::now();
    let mut found: Vec<(String, i64, String, Instant)> = {
        let mut b = app.beacons.lock().unwrap();
        b.sweep(now);
        let net = b.network_of(&ip);
        b.book.get(&net).map_or(vec![], |e| e.iter().map(|(k, v)| (k.clone(), v.0, v.1.clone(), v.2)).collect())
    };
    found.sort_by_key(|f| std::cmp::Reverse(f.3));
    let mut out = String::from("{\"stations\":[");
    for (i, (lan, port, name, seen)) in found.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        let mut o = Obj::new(&mut out);
        o.str("url", &format!("http://{lan}:{port}/"));
        write_str(o.key("name"), name);
        o.int("seen_s", now.duration_since(*seen).as_secs_f64().round_ties_even() as i64);
        o.end();
    }
    out.push_str("]}");
    json(out, "no-store")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn private_like_python_312() {
        for (a, want) in [
            ("192.168.1.23", true), ("10.0.0.2", true), ("172.31.255.1", true), ("100.64.0.1", false),
            ("192.0.0.9", false), ("192.0.0.10", false), ("192.0.0.8", true), ("240.0.0.1", true),
            ("255.255.255.255", true), ("0.0.0.0", false), ("169.254.1.1", false), ("127.0.0.1", false),
            ("198.18.0.1", true), ("192.0.0.171", true), ("8.8.8.8", false), ("010.1.1.1", false),
            ("1.2.3", false), (" 10.0.0.1", false), ("::1", false), ("fd00::1", false),
        ] {
            assert_eq!(lan_ok(a), want, "{a}");
        }
    }

    #[test]
    fn hostnames() {
        for ok in ["station", "a", "st-1", "0"] {
            assert!(hostname(ok), "{ok}");
        }
        for bad in ["", "-a", "a-", "bad_name", "Station", &"a".repeat(64)] {
            assert!(!hostname(bad), "{bad}");
        }
    }
}
