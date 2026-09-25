//! The stations registry (the Python service's stations.py, poller.py and
//! routes_stations.py): who feeds, since when, and roughly where.
//!
//! The privacy floor is the same. The full station UUID is never stored:
//! the registry keeps its sha256 (the self-view credential) and the
//! 16-hex half id readsb uses elsewhere. The feeder's address column in
//! clients.json is dropped at parse time. Locations are readsb's coverage
//! midpoints rounded to 0.1 degree, never anything the feeder typed.

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::{Path, Request, State};
use axum::response::{IntoResponse, Response};
use chrono::{DateTime, TimeDelta, Timelike, Utc};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::pg::Lazy;
use crate::http::{client_ip, json, peer_of, throttle, ApiError};
use crate::pyjson::{round_to, write_float, write_str, Obj};
use crate::state::{now_s, App};

/// Rounding of the public coordinates. Not configurable: the promise to
/// feeders does not vary by deployment.
const COARSE_DECIMALS: usize = 1;
/// A connection that restarted more than this after the open session
/// began is a new session (the TCP link dropped between polls).
const RECONNECT_S: i64 = 30;

/// 32 lowercase hex characters, dashes dropped; None for anything else.
pub fn normalize_uuid(uuid: &str) -> Option<String> {
    let n = uuid.trim().to_lowercase().replace('-', "");
    (n.len() == 32 && n.bytes().all(|b| b.is_ascii_hexdigit())).then_some(n)
}

/// readsb invents a half-zero UUID for connections that sent none
/// (internal plumbing: the mlat loop, upstream forwards). Not stations.
fn is_anonymous(uuid: &str) -> bool {
    normalize_uuid(uuid).is_none_or(|n| n.ends_with(&"0".repeat(16)))
}

fn sha256_hex(normalized: &str) -> String {
    Sha256::digest(normalized.as_bytes()).iter().map(|b| format!("{b:02x}")).collect()
}

/// A datetime the way Python's isoformat writes an aware UTC one.
pub fn isoformat(t: DateTime<Utc>) -> String {
    let micros = t.nanosecond() / 1000 % 1_000_000;
    let mut s = t.format("%Y-%m-%dT%H:%M:%S").to_string();
    if micros != 0 {
        s.push_str(&format!(".{micros:06}"));
    }
    s.push_str("+00:00");
    s
}

fn micros_now() -> DateTime<Utc> {
    DateTime::from_timestamp_micros(Utc::now().timestamp_micros()).unwrap()
}

/// Python's float() and int() of a JSON value, which is what the
/// registry applies to clients.json columns (a failure fails the poll).
fn float_of(v: &Value) -> anyhow::Result<f64> {
    match v {
        Value::Number(n) => n.as_f64().ok_or_else(|| anyhow::anyhow!("bad number")),
        Value::Bool(b) => Ok(*b as i64 as f64),
        Value::String(s) => Ok(s.trim().parse()?),
        _ => anyhow::bail!("not a number: {v}"),
    }
}

fn int_of(v: &Value) -> anyhow::Result<i64> {
    match v {
        Value::Number(n) => n
            .as_i64()
            .or_else(|| n.as_f64().map(|f| f.trunc() as i64))
            .ok_or_else(|| anyhow::anyhow!("bad number")),
        Value::Bool(b) => Ok(*b as i64),
        Value::String(s) => Ok(s.trim().parse()?),
        _ => anyhow::bail!("not an integer: {v}"),
    }
}

fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// One connected feeder in a clients.json poll.
#[derive(Clone, Debug, PartialEq)]
pub struct Client {
    pub uuid: String,
    pub kbit_s: f64,
    pub conn_time_s: f64,
    pub msgs_per_s: f64,
    pub positions_per_s: f64,
    pub rtt_ms: f64,
    pub positions_total: i64,
}

pub fn parse_clients(body: &[u8]) -> anyhow::Result<Vec<Client>> {
    let v: Value = serde_json::from_slice(body)?;
    let mut rows = vec![];
    for entry in v.get("clients").and_then(|c| c.as_array()).into_iter().flatten() {
        let Some(e) = entry.as_array().filter(|a| a.len() >= 9) else { continue };
        let Some(uuid) = e[0].as_str().filter(|u| !is_anonymous(u)) else { continue };
        rows.push(Client {
            uuid: uuid.to_string(),
            kbit_s: float_of(&e[2])?,
            conn_time_s: float_of(&e[3])?,
            msgs_per_s: float_of(&e[4])?,
            positions_per_s: float_of(&e[5])?,
            rtt_ms: float_of(&e[7])?,
            positions_total: int_of(&e[8])?,
        });
    }
    Ok(rows)
}

/// What the self view shows of a connected station. The UUID stays out
/// of process state, as it stays out of the database.
#[derive(Clone, Debug)]
pub struct Live {
    pub kbit_s: f64,
    pub msgs_per_s: f64,
    pub positions_per_s: f64,
    pub rtt_ms: f64,
    pub connected_since: String,
}

/// The registry: one connection for the pollers' writes, one for the
/// routes' reads, so a write never queues a page behind it.
pub struct Registry {
    writer: Lazy,
    reader: Lazy,
}

impl Registry {
    pub fn new(database_url: &str) -> Arc<Registry> {
        Arc::new(Registry { writer: Lazy::new(database_url), reader: Lazy::new(database_url) })
    }

    /// Apply one clients.json poll: create and update stations, keep
    /// their connection sessions, close the sessions of stations that
    /// left. Returns the presence map, keyed by half id.
    pub async fn upsert_presence(&self, rows: &[Client], now: DateTime<Utc>) -> anyhow::Result<HashMap<String, Live>> {
        let mut g = self.writer.get().await?;
        let tx = g.as_mut().unwrap().transaction().await?;
        let mut presence = HashMap::new();
        let mut seen: Vec<i64> = vec![];
        for row in rows {
            let Some(n) = normalize_uuid(&row.uuid) else { continue };
            let digest = sha256_hex(&n);
            let half_id = &n[..16];
            let found = tx.query_opt("SELECT id FROM stations WHERE uuid_sha256 = $1", &[&digest]).await?;
            let id: i64 = match found {
                Some(r) => r.get(0),
                None => tx
                    .query_one(
                        "INSERT INTO stations (public_id, uuid_sha256, half_id, first_seen, last_seen, positions_total) \
                         VALUES ($1, $2, $3, $4, $4, 0) RETURNING id",
                        &[&format!("fp-{}", &digest[..10]), &digest, &half_id, &now],
                    )
                    .await?
                    .get(0),
            };
            tx.execute(
                "UPDATE stations SET last_seen = $2, msgs_per_s = $3, positions_per_s = $4, kbit_s = $5, \
                 rtt_ms = $6, positions_total = GREATEST(positions_total, $7) WHERE id = $1",
                &[&id, &now, &row.msgs_per_s, &row.positions_per_s, &row.kbit_s, &row.rtt_ms, &row.positions_total],
            )
            .await?;
            seen.push(id);

            // timedelta(seconds=x) keeps microseconds, rounded half to even
            let started_at = now - TimeDelta::microseconds((row.conn_time_s * 1e6).round_ties_even() as i64);
            let open = tx
                .query_opt(
                    "SELECT id, started_at FROM station_sessions WHERE station_id = $1 AND ended_at IS NULL \
                     ORDER BY started_at DESC LIMIT 1",
                    &[&id],
                )
                .await?
                .map(|r| (r.get::<_, i64>(0), r.get::<_, DateTime<Utc>>(1)));
            let open = match open {
                Some((sid, began)) if started_at > began + TimeDelta::seconds(RECONNECT_S) => {
                    tx.execute("UPDATE station_sessions SET ended_at = last_seen_at WHERE id = $1", &[&sid]).await?;
                    None
                }
                other => other.map(|o| o.0),
            };
            match open {
                Some(sid) => {
                    tx.execute(
                        "UPDATE station_sessions SET last_seen_at = $2, \
                         peak_msgs_per_s = GREATEST(peak_msgs_per_s, $3), \
                         positions_total = GREATEST(positions_total, $4) WHERE id = $1",
                        &[&sid, &now, &row.msgs_per_s, &row.positions_total],
                    )
                    .await?;
                }
                None => {
                    tx.execute(
                        "INSERT INTO station_sessions (station_id, started_at, last_seen_at, peak_msgs_per_s, positions_total) \
                         VALUES ($1, $2, $3, $4, $5)",
                        &[&id, &started_at, &now, &row.msgs_per_s.max(0.0), &row.positions_total.max(0)],
                    )
                    .await?;
                }
            }
            presence.insert(
                half_id.to_string(),
                Live {
                    kbit_s: row.kbit_s,
                    msgs_per_s: row.msgs_per_s,
                    positions_per_s: row.positions_per_s,
                    rtt_ms: row.rtt_ms,
                    connected_since: isoformat(started_at),
                },
            );
        }
        // open sessions of stations absent from this poll: they left, at
        // the last moment we saw them
        tx.execute(
            "UPDATE station_sessions SET ended_at = last_seen_at WHERE ended_at IS NULL AND NOT (station_id = ANY($1))",
            &[&seen],
        )
        .await?;
        tx.commit().await?;
        Ok(presence)
    }

    pub async fn prune_sessions(&self, retention_days: i64, now: DateTime<Utc>) -> anyhow::Result<u64> {
        let g = self.writer.get().await?;
        let cutoff = now - TimeDelta::days(retention_days);
        Ok(g.as_ref()
            .unwrap()
            .execute("DELETE FROM station_sessions WHERE ended_at IS NOT NULL AND ended_at < $1", &[&cutoff])
            .await?)
    }

    /// receivers.json -> coarse locations: the midpoint of the coverage
    /// readsb derived from traffic, rounded. Rows flagged badExtent are
    /// skipped.
    pub async fn apply_receivers(&self, body: &[u8]) -> anyhow::Result<()> {
        let v: Value = serde_json::from_slice(body)?;
        let mut updates = vec![];
        for entry in v.get("receivers").and_then(|c| c.as_array()).into_iter().flatten() {
            let Some(e) = entry.as_array().filter(|a| a.len() >= 10) else { continue };
            let Some(id) = e[0].as_str() else { continue };
            if truthy(&e[7]) || e[8].is_null() || e[9].is_null() {
                continue;
            }
            let half_id: String = id.trim().to_lowercase().replace('-', "").chars().take(16).collect();
            let lat = round_to(float_of(&e[8])?, COARSE_DECIMALS);
            let lon = round_to(float_of(&e[9])?, COARSE_DECIMALS);
            updates.push((half_id, lat, lon));
        }
        let mut g = self.writer.get().await?;
        let tx = g.as_mut().unwrap().transaction().await?;
        for (half_id, lat, lon) in &updates {
            tx.execute("UPDATE stations SET coarse_lat = $2, coarse_lon = $3 WHERE half_id = $1", &[half_id, lat, lon])
                .await?;
        }
        tx.commit().await?;
        Ok(())
    }
}

// ---- pollers ----------------------------------------------------------------

/// clients.json -> the registry and the presence map. The map (and the
/// station count /v1/now reports) only moves once the write succeeded.
pub async fn poll_clients(app: &App, reg: &Registry, body: &[u8]) -> anyhow::Result<()> {
    let rows = parse_clients(body)?;
    let now = micros_now();
    let presence = reg.upsert_presence(&rows, now).await?;
    reg.prune_sessions(app.settings.session_retention_days, now).await?;
    let mut p = app.presence.lock().unwrap();
    p.count = presence.len();
    p.live = presence;
    p.at = now_s();
    Ok(())
}

pub async fn poll_receivers_once(app: &App, reg: &Registry) -> anyhow::Result<()> {
    match app.upstream.get("/data/receivers.json").await {
        Ok(body) => reg.apply_receivers(&body).await,
        Err(e) if crate::upstream::status_of(&e) == Some(404) => Ok(()),
        Err(e) => Err(e),
    }
}

// ---- routes -------------------------------------------------------------------

fn online(last_seen: DateTime<Utc>, offline_after_s: i64) -> bool {
    let age = Utc::now() - last_seen;
    age.num_microseconds().unwrap_or(i64::MAX) as f64 / 1e6 <= offline_after_s as f64
}

fn opt_f64(o: &mut Obj, k: &str, v: Option<f64>) {
    match v {
        Some(x) => write_float(o.key(k), x),
        None => o.key(k).push_str("null"),
    }
}

fn unavailable(e: impl std::fmt::Display) -> ApiError {
    eprintln!("stations: database: {e}");
    ApiError::new(503, "unavailable", "the stations registry is unavailable").header("Retry-After", "5")
}

/// GET /v1/stations: the public roster, oldest station first.
pub async fn roster(State(app): State<Arc<App>>, req: Request) -> Response {
    let Some(reg) = app.stations.clone() else {
        return crate::proxy::forward(State(app), req).await.into_response();
    };
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "stations", app.settings.stations_rate_limit) {
        return e.into_response();
    }
    let rows = {
        let g = match reg.reader.get().await {
            Ok(g) => g,
            Err(e) => return unavailable(e).into_response(),
        };
        match g
            .as_ref()
            .unwrap()
            .query(
                "SELECT public_id, label, coarse_lat, coarse_lon, first_seen, last_seen FROM stations ORDER BY first_seen",
                &[],
            )
            .await
        {
            Ok(rows) => rows,
            Err(e) => return unavailable(e).into_response(),
        }
    };
    let mut out = String::with_capacity(64 + rows.len() * 200);
    out.push_str("{\"stations\":[");
    for (i, r) in rows.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        let last_seen: DateTime<Utc> = r.get(5);
        let mut o = Obj::new(&mut out);
        o.str("id", r.get(0));
        match r.get::<_, Option<&str>>(1) {
            Some(l) => o.str("label", l),
            None => o.null("label"),
        };
        opt_f64(&mut o, "coarse_lat", r.get(2));
        opt_f64(&mut o, "coarse_lon", r.get(3));
        o.str("first_seen", &isoformat(r.get(4)))
            .str("last_seen", &isoformat(last_seen))
            .raw("online", if online(last_seen, app.settings.offline_after_s) { "true" } else { "false" });
        o.end();
    }
    out.push_str("]}");
    json(out, "public, s-maxage=30")
}

/// Aircraft one station sees now (readsb's filter_uuid takes the half id).
async fn aircraft_seen(app: &App, half_id: &str) -> Option<i64> {
    let body = app.upstream.get(&format!("/re-api/?all_with_pos&filter_uuid={half_id}")).await.ok()?;
    let v: Value = serde_json::from_slice(&body).ok()?;
    Some(v.get("aircraft").and_then(|a| a.as_array()).map_or(0, |a| a.len()) as i64)
}

/// GET /v1/stations/{uuid}: one feeder's own view. Knowing the full UUID
/// is the credential; malformed, unknown and wrong UUIDs are the same 404.
pub async fn self_view(State(app): State<Arc<App>>, Path(uuid): Path<String>, req: Request) -> Response {
    let Some(reg) = app.stations.clone() else {
        return crate::proxy::forward(State(app), req).await.into_response();
    };
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    if let Err(e) = throttle(&app, &ip, "station_detail", app.settings.station_detail_rate_limit) {
        return e.into_response();
    }
    let unknown = || ApiError::new(404, "not_found", "unknown station").into_response();
    let Some(n) = normalize_uuid(&uuid) else { return unknown() };
    let digest = sha256_hex(&n);
    let found = {
        let g = match reg.reader.get().await {
            Ok(g) => g,
            Err(e) => return unavailable(e).into_response(),
        };
        let c = g.as_ref().unwrap();
        let station = match c
            .query_opt(
                "SELECT id, public_id, half_id, first_seen, last_seen, positions_total FROM stations WHERE uuid_sha256 = $1",
                &[&digest],
            )
            .await
        {
            Ok(s) => s,
            Err(e) => return unavailable(e).into_response(),
        };
        match station {
            None => None,
            Some(s) => {
                // the sessions in the order the table holds them, as the
                // Python service loads them before sorting
                let sessions = match c
                    .query(
                        "SELECT started_at, ended_at, peak_msgs_per_s, positions_total FROM station_sessions \
                         WHERE station_id = $1",
                        &[&s.get::<_, i64>(0)],
                    )
                    .await
                {
                    Ok(r) => r,
                    Err(e) => return unavailable(e).into_response(),
                };
                Some((s, sessions))
            }
        }
    };
    let Some((s, mut sessions)) = found else { return unknown() };
    let half_id: String = s.get(2);
    let live = app.presence.lock().unwrap().live.get(&half_id).cloned();
    let seen = match &live {
        Some(_) => aircraft_seen(&app, &half_id).await,
        None => None,
    };
    let last_seen: DateTime<Utc> = s.get(4);
    // newest first; equal starts keep their order (a stable sort)
    sessions.sort_by_key(|r| std::cmp::Reverse(r.get::<_, DateTime<Utc>>(0)));

    let mut out = String::with_capacity(1024);
    let mut o = Obj::new(&mut out);
    o.str("id", s.get(1))
        .raw("online", if online(last_seen, app.settings.offline_after_s) { "true" } else { "false" });
    match &live {
        Some(l) => o.str("connected_since", &l.connected_since),
        None => o.null("connected_since"),
    };
    opt_f64(&mut o, "messages_per_s", live.as_ref().map(|l| l.msgs_per_s));
    opt_f64(&mut o, "positions_per_s", live.as_ref().map(|l| l.positions_per_s));
    opt_f64(&mut o, "kbit_s", live.as_ref().map(|l| l.kbit_s));
    opt_f64(&mut o, "rtt_ms", live.as_ref().map(|l| l.rtt_ms));
    o.int("positions_total", s.get(5));
    match seen {
        Some(n) => o.int("aircraft_seen", n),
        None => o.null("aircraft_seen"),
    };
    o.str("first_seen", &isoformat(s.get(3))).str("last_seen", &isoformat(last_seen));
    let list = o.key("recent_sessions");
    list.push('[');
    for (i, r) in sessions.iter().take(10).enumerate() {
        if i > 0 {
            list.push(',');
        }
        list.push_str("{\"started_at\":");
        write_str(list, &isoformat(r.get(0)));
        list.push_str(",\"ended_at\":");
        match r.get::<_, Option<DateTime<Utc>>>(1) {
            Some(t) => write_str(list, &isoformat(t)),
            None => list.push_str("null"),
        }
        list.push_str(",\"peak_messages_per_s\":");
        write_float(list, r.get(2));
        list.push_str(&format!(",\"positions_total\":{}}}", r.get::<_, i64>(3)));
    }
    list.push(']');
    o.end();
    json(out, "no-store")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_clients_and_drops_the_address() {
        let body = br#"{"clients":[
            ["0123-4567-89AB-CDEF-0123456789abcdef","1.2.3.4 port 5",12.5,3600,40,20,0,31.2,1234],
            ["abcdef0123456789-0000-0000-000000000000","x",1,2,3,4,5,6,7],
            ["short","x",1,2,3,4,5,6,7],
            ["fedcba98765432100123456789abcdef","x",1]
        ]}"#;
        let rows = parse_clients(body).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].kbit_s, 12.5);
        assert_eq!(rows[0].msgs_per_s, 40.0);
        assert_eq!(rows[0].rtt_ms, 31.2);
        assert_eq!(rows[0].positions_total, 1234);
        assert!(!format!("{:?}", rows[0]).contains("1.2.3.4"));
    }

    #[test]
    fn identities_like_the_python_registry() {
        let n = normalize_uuid(" 0123-4567-89AB-CDEF-0123456789ABCDEF ").unwrap();
        assert_eq!(n, "0123456789abcdef0123456789abcdef");
        // hashlib.sha256(b"0123456789abcdef0123456789abcdef").hexdigest()
        assert_eq!(sha256_hex(&n), "3eb1bd439947eb762998e566ccc2e099c791118b2f40579cc4f7da2b5061b7f9");
        assert!(normalize_uuid("0123456789abcdef0123456789abcdeg").is_none());
        assert!(is_anonymous("abcdef0123456789-0000-0000-000000000000"));
    }

    #[test]
    fn isoformat_like_python() {
        let t = DateTime::from_timestamp_micros(1_790_000_000_123_456).unwrap();
        assert_eq!(isoformat(t), "2026-09-21T14:13:20.123456+00:00");
        let t = DateTime::from_timestamp_micros(1_790_000_000_000_000).unwrap();
        assert_eq!(isoformat(t), "2026-09-21T14:13:20+00:00");
        let t = DateTime::from_timestamp_micros(1_790_000_000_000_010).unwrap();
        assert_eq!(isoformat(t), "2026-09-21T14:13:20.000010+00:00");
    }

    /// Replays a poll scenario (STATIONS_SCENARIO) on STATIONS_TEST_DB and
    /// writes each poll's presence map to STATIONS_OUT, for comparison
    /// with the Python registry replaying the same polls.
    #[tokio::test]
    async fn replays_a_scenario() {
        let (Ok(url), Ok(path), Ok(out)) =
            (std::env::var("STATIONS_TEST_DB"), std::env::var("STATIONS_SCENARIO"), std::env::var("STATIONS_OUT"))
        else {
            return;
        };
        let reg = Registry::new(&url);
        let polls: Vec<Value> = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        let mut maps = vec![];
        for p in polls {
            let now = DateTime::from_timestamp_micros(p["now"].as_i64().unwrap()).unwrap();
            let rows = parse_clients(p["clients"].to_string().as_bytes()).unwrap();
            let presence = reg.upsert_presence(&rows, now).await.unwrap();
            reg.prune_sessions(90, now).await.unwrap();
            reg.apply_receivers(p["receivers"].to_string().as_bytes()).await.unwrap();
            let mut keys: Vec<_> = presence.keys().cloned().collect();
            keys.sort();
            let mut m = String::from("{");
            for (i, k) in keys.iter().enumerate() {
                let l = &presence[k];
                if i > 0 {
                    m.push_str(", ");
                }
                write_str(&mut m, k);
                m.push_str(": {\"kbit_s\": ");
                write_float(&mut m, l.kbit_s);
                m.push_str(", \"msgs_per_s\": ");
                write_float(&mut m, l.msgs_per_s);
                m.push_str(", \"positions_per_s\": ");
                write_float(&mut m, l.positions_per_s);
                m.push_str(", \"rtt_ms\": ");
                write_float(&mut m, l.rtt_ms);
                m.push_str(", \"connected_since\": ");
                write_str(&mut m, &l.connected_since);
                m.push('}');
            }
            m.push('}');
            maps.push(m);
        }
        std::fs::write(out, format!("[{}]\n", maps.join(", "))).unwrap();
    }
}
