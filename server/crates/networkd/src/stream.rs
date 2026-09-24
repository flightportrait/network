//! The live stream. One socket per open map. The client says which box
//! it looks at (or null for the whole sky); the server answers with
//! everything in it, then on every new snapshot only what changed:
//! aircraft that moved or changed (`upd`) and aircraft that left the box
//! or the sky (`del`). `seen`/`seen_pos` ticking alone is not a change.
//!
//! Two dialects, same envelope, same timing:
//!
//! - `/v1/stream`: every `upd` entry is the aircraft's whole object, and
//!   a new box resends everything in it (`"full": true`). Compatible with
//!   the Python service.
//! - `/v2/stream`: an `upd` entry for an aircraft the client already
//!   holds carries only `hex`, the fields that changed (`null`: the field
//!   is gone), and `seen`/`seen_pos`; merge it into the held object. An
//!   aircraft new to the client arrives whole. A new box sends only what
//!   the client does not hold yet. `"full": true` marks the first message.
//!   Same data, same freshness, a fraction of the bytes.
//!
//! Differences from the Python service, all invisible to a client: the
//! order of hexes inside `del`, and permessage-deflate negotiated
//! without context takeover.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::extract::{Request, State};
use axum::http::{Response, StatusCode};
use axum::response::IntoResponse;
use hyper_util::rt::TokioIo;
use serde_json::Value;
use tokio::io::AsyncWriteExt;
use tokio::sync::mpsc;

use crate::http::{client_ip, not_found, peer_of};
use crate::live::parse_bbox;
use crate::pyjson::{write_float, write_str, Obj};
use crate::sky::{BBox, Entry, Snapshot, FIELDS, F_HEX, F_SEEN, F_SEEN_POS};
use crate::state::App;
use crate::ws::{self, Incoming};

const TICK: Duration = Duration::from_secs(1);
const FIRST_WAIT: Duration = Duration::from_millis(500);

#[derive(Clone, Copy, PartialEq, Debug)]
pub enum Dialect {
    V1,
    V2,
}

pub async fn stream_v1(State(app): State<Arc<App>>, req: Request) -> Response<Body> {
    open(app, req, Dialect::V1).await
}

pub async fn stream_v2(State(app): State<Arc<App>>, req: Request) -> Response<Body> {
    open(app, req, Dialect::V2).await
}

async fn open(app: Arc<App>, req: Request, dialect: Dialect) -> Response<Body> {
    let Some(hs) = ws::handshake(req.headers(), app.settings.deflate_takeover) else {
        return not_found().await.into_response();
    };
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    {
        let mut open = app.open_sockets.lock().unwrap();
        let n = open.entry(ip.clone()).or_insert(0);
        if *n >= app.settings.stream_max_per_ip {
            // closed before accept: the HTTP 403 a refused socket gets
            let mut r = Response::new(Body::empty());
            *r.status_mut() = StatusCode::FORBIDDEN;
            return r;
        }
        *n += 1;
    }
    let guard = SocketCount { app: app.clone(), ip };
    let (deflate, takeover) = (hs.deflate, hs.takeover);
    let upgrade = hyper::upgrade::on(req);
    tokio::spawn(async move {
        let _guard = guard;
        if let Ok(io) = upgrade.await {
            let _ = serve(app.clone(), TokioIo::new(io), deflate, takeover, dialect).await;
        }
    });
    hs.response
}

struct SocketCount {
    app: Arc<App>,
    ip: String,
}

impl Drop for SocketCount {
    fn drop(&mut self) {
        let mut open = self.app.open_sockets.lock().unwrap();
        if let Some(n) = open.get_mut(&self.ip) {
            *n -= 1;
            if *n == 0 {
                open.remove(&self.ip);
            }
        }
    }
}

enum FromClient {
    Json(Value),
    /// Not JSON, binary, or broken framing: close with this code.
    Invalid(u16),
    Ping(Vec<u8>),
    Gone,
}

/// `{"bbox": [w, s, e, n] | null}` -> box. Anything else that parses is a
/// whole-sky request, like the Python service.
fn box_of(msg: &Value) -> Result<Option<BBox>, ()> {
    let Some(b) = msg.as_object().and_then(|o| o.get("bbox")) else { return Ok(None) };
    if b.is_null() {
        return Ok(None);
    }
    let a = b.as_array().filter(|a| a.len() == 4).ok_or(())?;
    let mut parts = Vec::with_capacity(4);
    for x in a {
        // Python's str(float(x)): numbers, numeric strings, booleans
        let f = match x {
            Value::Number(n) => n.as_f64().ok_or(())?,
            Value::String(s) => crate::live::py_float(s).ok_or(())?,
            Value::Bool(b) => *b as i64 as f64,
            _ => return Err(()),
        };
        let mut s = String::new();
        write_float(&mut s, f);
        parts.push(s);
    }
    parse_bbox(&parts.join(",")).map(Some).map_err(|_| ())
}

/// What one client holds, and the next message it needs.
pub struct View {
    dialect: Dialect,
    /// hex -> (signature, the round it was last in view)
    sent: HashMap<Arc<str>, (u64, u32)>,
    round: u32,
    /// The snapshot the client's objects come from (v2 diffs against it).
    prev: Option<Arc<Snapshot>>,
    idx: Vec<u32>,
}

impl View {
    pub fn new(dialect: Dialect) -> View {
        View { dialect, sent: HashMap::new(), round: 0, prev: None, idx: Vec::new() }
    }

    pub fn holds_nothing(&self) -> bool {
        self.sent.is_empty()
    }

    /// The message for `snap` seen through `bbox`, built after
    /// `ws::HEADROOM` bytes, or None when nothing changed. `full`: the
    /// first message, or (v1) a new box.
    pub fn render(&mut self, snap: &Arc<Snapshot>, bbox: Option<&BBox>, full: bool) -> Option<String> {
        self.round = self.round.wrapping_add(1);
        let round = self.round;
        let v2 = self.dialect == Dialect::V2;
        let resend_all = full && !v2;
        let mut out = String::with_capacity(4096);
        out.push_str(std::str::from_utf8(&[b' '; ws::HEADROOM]).unwrap());
        let mut upd_n = 0usize;
        let mut o = Obj::new(&mut out);
        o.f64("t", snap.generated_at).int("total", snap.count() as i64).int("with_position", snap.with_pos as i64);
        let buf = o.key("upd");
        buf.push('[');
        let listed: Box<dyn Iterator<Item = &Entry>> = match bbox {
            None => Box::new(snap.entries.iter()),
            Some(b) => {
                snap.in_box(b, &mut self.idx);
                Box::new(self.idx.iter().map(|&i| &snap.entries[i as usize]))
            }
        };
        for e in listed {
            if e.hex.is_empty() {
                continue;
            }
            // held: Some(changed?) for an aircraft the client has
            let held = match self.sent.get_mut(&e.hex) {
                Some(s) => {
                    let changed = s.0 != e.sig;
                    *s = (e.sig, round);
                    Some(changed)
                }
                None => {
                    self.sent.insert(e.hex.clone(), (e.sig, round));
                    None
                }
            };
            let send = match held {
                None => true,
                Some(changed) => changed || resend_all,
            };
            if !send {
                continue;
            }
            if upd_n > 0 {
                buf.push(',');
            }
            upd_n += 1;
            let before = match (v2, held) {
                (true, Some(true)) => self.prev.as_ref().and_then(|p| p.find(&e.hex)),
                _ => None,
            };
            match before {
                Some(p) => write_patch(buf, p, e),
                None => buf.push_str(&e.json),
            }
        }
        buf.push(']');
        let buf = o.key("del");
        buf.push('[');
        let mut del_n = 0usize;
        self.sent.retain(|h, s| {
            if s.1 == round {
                return true;
            }
            if del_n > 0 {
                buf.push(',');
            }
            write_str(buf, h);
            del_n += 1;
            false
        });
        buf.push(']');
        if full {
            o.raw("full", "true");
        }
        o.end();
        if self.sent.capacity() > 4 * self.sent.len() + 64 {
            self.sent.shrink_to_fit();
        }
        if self.idx.capacity() > 4 * self.idx.len() + 1024 {
            self.idx = Vec::new();
        }
        self.prev = Some(snap.clone());
        (full || upd_n > 0 || del_n > 0).then_some(out)
    }
}

/// `{"hex":…, <changed fields>, "seen":…, "seen_pos":…}`; a field that
/// disappeared is written as null.
fn write_patch(out: &mut String, before: &Entry, now: &Entry) {
    out.push('{');
    out.push_str(now.field(F_HEX).unwrap_or("\"hex\":\"\""));
    for (i, name) in FIELDS.iter().enumerate() {
        if i == F_HEX {
            continue;
        }
        let (a, b) = (before.field(i), now.field(i));
        let always = i == F_SEEN || i == F_SEEN_POS;
        match b {
            Some(seg) if always || a != Some(seg) => {
                out.push(',');
                out.push_str(seg);
            }
            None if a.is_some() && !always => {
                out.push(',');
                write_str(out, name);
                out.push_str(":null");
            }
            _ => {}
        }
    }
    out.push('}');
}

async fn serve<S>(app: Arc<App>, io: S, deflate: bool, takeover: bool, dialect: Dialect) -> std::io::Result<()>
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let (mut rd, mut wr) = tokio::io::split(io);
    let (tx, mut rx) = mpsc::channel::<FromClient>(4);
    let reader = tokio::spawn(async move {
        loop {
            let m = match ws::read_message(&mut rd, deflate).await {
                Ok(Incoming::Text(t)) => match serde_json::from_slice::<Value>(&t) {
                    Ok(v) => FromClient::Json(v),
                    Err(_) => FromClient::Invalid(1003),
                },
                Ok(Incoming::Binary) => FromClient::Invalid(1003),
                Ok(Incoming::Fail(code)) => FromClient::Invalid(code),
                Ok(Incoming::Ping(p)) => FromClient::Ping(p),
                Ok(Incoming::Close) | Err(_) => FromClient::Gone,
            };
            let last = matches!(m, FromClient::Gone | FromClient::Invalid(_));
            if tx.send(m).await.is_err() || last {
                break;
            }
        }
    });

    let mut published = app.subscribe();
    let mut bbox: Option<BBox> = None;
    let mut view = View::new(dialect);
    let mut outgoing = ws::Out::new(deflate, takeover);
    let mut last_gen = -1.0f64;
    let mut wait = FIRST_WAIT;

    let result = async {
        loop {
            let mut msg = None;
            tokio::select! {
                m = rx.recv() => match m {
                    Some(FromClient::Json(v)) => msg = Some(v),
                    Some(FromClient::Ping(p)) => {
                        wr.write_all(&ws::frame(ws::OP_PONG, &p, false)).await?;
                        continue;
                    }
                    Some(FromClient::Invalid(code)) => {
                        wr.write_all(&ws::close_frame(code)).await?;
                        return Ok(());
                    }
                    Some(FromClient::Gone) | None => return Ok(()),
                },
                r = published.changed() => { if r.is_err() { return Ok(()); } }
                _ = tokio::time::sleep(wait) => {}
            }
            wait = TICK;
            let mut full = false;
            let mut new_view = false;
            if let Some(m) = msg {
                let Ok(new_box) = box_of(&m) else {
                    wr.write_all(&ws::close_frame(1003)).await?;
                    return Ok(());
                };
                if new_box != bbox || (view.holds_nothing() && last_gen < 0.0) {
                    bbox = new_box;
                    full = dialect == Dialect::V1;
                    new_view = true;
                }
            }
            let snap = app.snapshot();
            if !snap.fresh(app.settings.stale_after_s) {
                if last_gen != 0.0 {
                    wr.write_all(&outgoing.text(b"{\"stale\":true}")).await?;
                    last_gen = 0.0;
                }
                continue;
            }
            if snap.generated_at == last_gen && !new_view {
                continue;
            }
            if last_gen < 0.0 {
                full = true; // the first word is always everything
            }
            last_gen = snap.generated_at;
            let frame = match view.render(&snap, bbox.as_ref(), full) {
                Some(out) => outgoing.text_in_place(out.into_bytes()),
                None => {
                    // nothing moved: still say the sky was heard
                    let mut t = String::from("{\"t\":");
                    write_float(&mut t, snap.generated_at);
                    t.push('}');
                    outgoing.text(t.as_bytes())
                }
            };
            drop(snap);
            wr.write_all(&frame).await?;
        }
    }
    .await;
    reader.abort();
    let _ = wr.shutdown().await;
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sky::parse_aircraft;
    use rand::{Rng, SeedableRng};
    use serde_json::Map;

    #[test]
    fn box_messages() {
        let v = |s: &str| serde_json::from_str::<Value>(s).unwrap();
        assert_eq!(box_of(&v(r#"{"bbox":null}"#)), Ok(None));
        assert_eq!(box_of(&v(r#"[1,2]"#)), Ok(None));
        assert_eq!(box_of(&v(r#"{"bbox":[1,2,3,4]}"#)), Ok(Some(BBox { w: 1.0, s: 2.0, e: 3.0, n: 4.0 })));
        assert_eq!(box_of(&v(r#"{"bbox":["1.5",2,3,4]}"#)).unwrap().unwrap().w, 1.5);
        assert!(box_of(&v(r#"{"bbox":[1,2,3]}"#)).is_err());
        assert!(box_of(&v(r#"{"bbox":[1,5,3,4]}"#)).is_err());
        assert!(box_of(&v(r#"{"bbox":[1,{},3,4]}"#)).is_err());
    }

    /// The map's handling of a message: v1 replaces objects, v2 merges.
    fn apply(held: &mut HashMap<String, Map<String, Value>>, msg: &str, dialect: Dialect) {
        let m: Value = serde_json::from_str(msg.trim_start()).unwrap();
        if dialect == Dialect::V1 && m.get("full") == Some(&Value::Bool(true)) {
            held.clear();
        }
        for u in m["upd"].as_array().unwrap() {
            let u = u.as_object().unwrap();
            let hex = u["hex"].as_str().unwrap().to_string();
            match dialect {
                Dialect::V1 => {
                    held.insert(hex, u.clone());
                }
                Dialect::V2 => {
                    let o = held.entry(hex).or_default();
                    for (k, v) in u {
                        if v.is_null() {
                            o.remove(k);
                        } else {
                            o.insert(k.clone(), v.clone());
                        }
                    }
                }
            }
        }
        for h in m["del"].as_array().unwrap() {
            held.remove(h.as_str().unwrap());
        }
    }

    /// What the client knows about an aircraft, as the Python service's
    /// change test sees it: nulls and absent fields alike, and the ages
    /// (`seen`, `seen_pos`) left out, since a client ages each aircraft
    /// from the moment it last received it.
    fn known(o: &Map<String, Value>) -> Map<String, Value> {
        o.iter()
            .filter(|(k, v)| !v.is_null() && *k != "seen" && *k != "seen_pos")
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect()
    }

    /// A v2 client, merging patches, ends every round knowing exactly
    /// what a v1 client knows, while receiving fewer bytes.
    #[test]
    fn v2_patches_rebuild_what_v1_sends() {
        let mut rng = rand_chacha::ChaCha8Rng::seed_from_u64(11);
        let hexes: Vec<String> = (0..60).map(|i| format!("{:06x}", 0xa00000 + i)).collect();
        let mut state: HashMap<String, Map<String, Value>> = HashMap::new();
        let (mut v1, mut v2) = (View::new(Dialect::V1), View::new(Dialect::V2));
        let (mut held1, mut held2) = (HashMap::new(), HashMap::new());
        let (mut bytes1, mut bytes2) = (0usize, 0usize);
        let boxes = [None, Some(BBox { w: -10.0, s: -10.0, e: 10.0, n: 10.0 }), Some(BBox { w: 0.0, s: 0.0, e: 30.0, n: 30.0 })];
        let mut bbox = None;
        for round in 0..300 {
            // the sky moves: some aircraft change fields, appear or vanish
            for h in &hexes {
                if rng.gen_bool(0.3) {
                    let o = state.entry(h.clone()).or_insert_with(|| {
                        let mut m = Map::new();
                        m.insert("hex".into(), Value::String(h.clone()));
                        m
                    });
                    for (k, gen) in [
                        ("flight", 0.05), ("lat", 0.6), ("lon", 0.6), ("alt_baro", 0.3),
                        ("gs", 0.4), ("track", 0.2), ("squawk", 0.02), ("seen", 1.0),
                    ] {
                        if rng.gen_bool(gen) {
                            let v = match k {
                                "flight" if rng.gen_bool(0.2) => Value::Null,
                                "flight" => Value::String(format!("AB{}", rng.gen_range(1..99))),
                                "squawk" => Value::String(format!("{:04}", rng.gen_range(1000..7777))),
                                "alt_baro" if rng.gen_bool(0.1) => Value::String("ground".into()),
                                "lat" | "lon" => serde_json::json!(rng.gen_range(-40.0..40.0f64)),
                                _ => serde_json::json!(rng.gen_range(0..40000)),
                            };
                            o.insert(k.into(), v);
                        } else if k == "track" && rng.gen_bool(0.05) {
                            o.remove(k);
                        }
                    }
                } else if rng.gen_bool(0.02) {
                    state.remove(h);
                }
            }
            if round % 25 == 0 {
                bbox = boxes[rng.gen_range(0..boxes.len())];
            }
            let snap = Arc::new(Snapshot::new(
                round as f64 + 1.0,
                state.values().map(|o| Entry::new(parse_aircraft(&Value::Object(o.clone()).to_string()).unwrap())).collect(),
            ));
            let full = round == 0;
            let new_box = round % 25 == 0;
            if let Some(m) = v1.render(&snap, bbox.as_ref(), full || new_box) {
                bytes1 += m.len();
                apply(&mut held1, &m, Dialect::V1);
            }
            if let Some(m) = v2.render(&snap, bbox.as_ref(), full) {
                bytes2 += m.len();
                apply(&mut held2, &m, Dialect::V2);
            }
            let norm = |h: &HashMap<String, Map<String, Value>>| -> HashMap<String, Map<String, Value>> {
                h.iter().map(|(k, v)| (k.clone(), known(v))).collect()
            };
            assert_eq!(norm(&held1), norm(&held2), "round {round}");
        }
        assert!(bytes2 < bytes1, "v2 {bytes2} bytes, v1 {bytes1}");
        eprintln!("v1 {bytes1} bytes, v2 {bytes2} bytes");
    }
}
