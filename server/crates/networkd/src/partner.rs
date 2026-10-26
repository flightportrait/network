//! Partner data: the live sky of a peer network that feeds us under its
//! own agreement, not the feeder terms. It may drive the frames and
//! nothing public: no public route, stream, trace, estimate, squawk,
//! station or release reads it. The fleet tier is its only reader,
//! through `fleet_sky`, which lays the partner's aircraft the network
//! did not hear after the network's own.
//!
//! The partner's stream is its own readsb (NETWORKD_PARTNER_LIVE_JSON,
//! that readsb's JSON position port), never merged into the network's
//! aggregator. The test at the end holds the rule in the source: only
//! this module, the fleet tier and the wiring name it.
//!
//! One public door, by agreement: `map`, the map's partner layer. Live
//! positions of the partner's aircraft the network does not hear, for
//! our own site only, credited, never cached, never in the OpenAPI.

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::{Request, State};
use axum::http::header;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpStream;

use crate::http::{client_ip, json, peer_of, query_param, throttle, ApiError, ApiResult};
use crate::live::parse_bbox;
use crate::pyjson::{write_float, write_str};
use crate::sky::{parse_aircraft_full, Entry, LiveSky, Snapshot, SnapshotCell, F_HEX, F_FLIGHT, F_LAT, F_LON, F_ALT, F_TRACK};
use crate::state::{now_s, App};

/// How often the partner's snapshot is rebuilt while its lines flow.
const PUBLISH_S: f64 = 1.0;
/// What the map's partner layer carries of each aircraft: identity and
/// position (hex, flight, t, lat, lon, alt_baro, gs, track, category).
const MAP_FIELDS: [usize; 9] = [F_HEX, F_FLIGHT, 2, F_LAT, F_LON, F_ALT, 7, F_TRACK, 9];
const MAP_MAX: usize = 3000;
const NO_STORE: &str = "no-store";

pub struct Partner {
    live: Mutex<LiveSky>,
    snapshot: SnapshotCell,
    merged: Mutex<Option<Merged>>,
}

/// The last merge, kept with the two snapshots it was made from.
struct Merged {
    network: Arc<Snapshot>,
    partner: Arc<Snapshot>,
    sky: Arc<Snapshot>,
}

impl Partner {
    pub fn new(max_aircraft: usize) -> Arc<Partner> {
        Arc::new(Partner {
            live: Mutex::new(LiveSky::new(max_aircraft)),
            snapshot: SnapshotCell::new(),
            merged: Mutex::new(None),
        })
    }

    pub fn set_snapshot(&self, s: Arc<Snapshot>) {
        self.snapshot.set(s);
    }

    /// The network's snapshot, then the partner's aircraft it does not
    /// hold, while the partner's snapshot is fresh. A stale or absent
    /// partner leaves the network's snapshot as it is.
    pub fn fleet_sky(&self, network: Arc<Snapshot>, stale_after_s: i64) -> Arc<Snapshot> {
        let partner = self.snapshot.get();
        if !partner.fresh(stale_after_s) {
            return network;
        }
        let mut merged = self.merged.lock().unwrap();
        if let Some(m) = merged.as_ref() {
            if Arc::ptr_eq(&m.network, &network) && Arc::ptr_eq(&m.partner, &partner) {
                return m.sky.clone();
            }
        }
        let known: HashSet<&str> = network.entries.iter().map(|e| &*e.hex).collect();
        let copy = |e: &Entry| Entry::with_hidden((*e.vals).clone(), e.hidden.clone());
        let mut entries: Vec<Entry> = network.entries.iter().map(copy).collect();
        entries.extend(partner.entries.iter().filter(|e| !known.contains(&*e.hex)).map(copy));
        let sky = Arc::new(Snapshot::new(network.generated_at, entries));
        *merged = Some(Merged { network, partner, sky: sky.clone() });
        sky
    }
}

/// Keep one connection to the partner readsb's JSON port; merge every
/// line into the partner's own state.
pub async fn read_lines(partner: Arc<Partner>, endpoint: String) {
    let mut backoff = 1.0f64;
    loop {
        match tokio::time::timeout(Duration::from_secs(10), TcpStream::connect(&endpoint)).await {
            Ok(Ok(sock)) => {
                {
                    let mut live = partner.live.lock().unwrap();
                    live.connected = true;
                    live.connected_at = now_s();
                }
                backoff = 1.0;
                eprintln!("partner sky connected to {endpoint}");
                let _ = sock.set_nodelay(true);
                let mut reader = BufReader::with_capacity(1 << 16, sock);
                let mut line = Vec::with_capacity(1024);
                loop {
                    line.clear();
                    match reader.read_until(b'\n', &mut line).await {
                        Ok(0) | Err(_) => break,
                        Ok(_) => {}
                    }
                    let Ok(text) = std::str::from_utf8(&line) else { continue };
                    let Some((vals, hidden)) = parse_aircraft_full(text.trim_end()) else { continue };
                    partner.live.lock().unwrap().ingest_full(vals, hidden, now_s());
                }
                partner.live.lock().unwrap().connected = false;
            }
            Ok(Err(e)) => eprintln!("partner sky {endpoint}: {e}"),
            Err(_) => eprintln!("partner sky {endpoint}: connect timed out"),
        }
        eprintln!("partner sky reconnecting in {backoff:.0} s");
        tokio::time::sleep(Duration::from_secs_f64(backoff)).await;
        backoff = (backoff * 2.0).min(30.0);
    }
}

/// While the partner's lines flow, rebuild its snapshot on change. When
/// they stop, the snapshot ages out and the fleet tier stops using it.
pub async fn publish(partner: Arc<Partner>) {
    let mut seen_version = 0;
    let mut tick = tokio::time::interval(Duration::from_secs_f64(PUBLISH_S));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tick.tick().await;
        let now = now_s();
        let snap = {
            let mut live = partner.live.lock().unwrap();
            if live.version == seen_version || !live.fresh(now) {
                continue;
            }
            seen_version = live.version;
            Arc::new(live.snapshot(now))
        };
        partner.set_snapshot(snap);
    }
}

/// The request comes from one of the site's own pages: its Origin, or
/// failing that its Referer, is a listed origin.
fn from_site(origins: &[String], headers: &axum::http::HeaderMap) -> bool {
    let get = |h| headers.get(h).and_then(|v| v.to_str().ok());
    if let Some(o) = get(header::ORIGIN) {
        return origins.iter().any(|x| x == o.trim_end_matches('/'));
    }
    get(header::REFERER).is_some_and(|r| origins.iter().any(|x| r == x || r.starts_with(&format!("{x}/"))))
}

/// GET /v1/map/partner?bbox=: the partner's live aircraft the network
/// does not hear, for our map. Positions only: no history, no search, no
/// fields beyond MAP_FIELDS. Answered only to the site's own pages,
/// throttled, `no-store`. A browser page elsewhere gets 403; a scraper
/// can forge an Origin, which is why the agreement promises "never
/// offered", not "impossible to copy".
pub async fn map(State(app): State<Arc<App>>, req: Request) -> ApiResult {
    let s = &app.settings;
    let partner = match &app.partner {
        Some(p) if !s.partner_name.is_empty() => p,
        _ => return Err(ApiError::new(404, "not_found", "Not Found")),
    };
    if !from_site(&s.partner_map_origins, req.headers()) {
        return Err(ApiError::new(403, "forbidden", "this layer belongs to the FlightPortrait map"));
    }
    let ip = client_ip(&app, req.headers(), peer_of(&req));
    throttle(&app, &ip, "partner_map", s.partner_map_rate_limit)?;
    let bbox = match query_param(req.uri().query(), "bbox").as_deref() {
        Some(b) if b.chars().count() > 80 => {
            return Err(ApiError::new(422, "invalid_request", "query.bbox: String should have at most 80 characters"))
        }
        Some(b) if !b.is_empty() => Some(parse_bbox(b)?),
        _ => None,
    };
    let theirs = partner.snapshot.get();
    let ours = app.snapshot();
    let mut body = String::with_capacity(4096);
    body.push_str("{\"credit\":{\"name\":");
    write_str(&mut body, &s.partner_name);
    body.push_str(",\"url\":");
    write_str(&mut body, &s.partner_url);
    body.push_str("},\"now\":");
    write_float(&mut body, theirs.generated_at);
    body.push_str(",\"aircraft\":[");
    if theirs.fresh(s.stale_after_s) {
        let known: HashSet<&str> = ours.entries.iter().map(|e| &*e.hex).collect();
        let mut n = 0;
        for e in &theirs.entries {
            if !e.has_pos || known.contains(&*e.hex) || bbox.as_ref().is_some_and(|b| !e.in_bbox(b)) {
                continue;
            }
            if n > 0 {
                body.push(',');
            }
            body.push('{');
            let mut first = true;
            for f in MAP_FIELDS {
                if let Some(kv) = e.field(f) {
                    if !first {
                        body.push(',');
                    }
                    body.push_str(kv);
                    first = false;
                }
            }
            body.push('}');
            n += 1;
            if n >= MAP_MAX {
                break;
            }
        }
    }
    body.push_str("]}");
    Ok(json(body, NO_STORE))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snap(at: f64, rows: &[&str]) -> Arc<Snapshot> {
        let entries = rows
            .iter()
            .map(|r| {
                let (v, h) = parse_aircraft_full(r).unwrap();
                Entry::with_hidden(v, h)
            })
            .collect();
        Arc::new(Snapshot::new(at, entries))
    }

    #[test]
    fn the_partner_adds_only_what_the_network_did_not_hear() {
        let p = Partner::new(1000);
        let network = snap(now_s(), &[r#"{"hex":"aaaaaa","lat":1.0,"lon":103.0}"#]);
        // nothing from the partner yet: the network's own snapshot
        assert!(Arc::ptr_eq(&p.fleet_sky(network.clone(), 30), &network));
        p.set_snapshot(snap(
            now_s(),
            &[r#"{"hex":"aaaaaa","lat":9.0,"lon":9.0}"#, r#"{"hex":"bbbbbb","lat":1.1,"lon":103.1}"#],
        ));
        let sky = p.fleet_sky(network.clone(), 30);
        let hexes: Vec<&str> = sky.entries.iter().map(|e| &*e.hex).collect();
        assert_eq!(hexes, ["aaaaaa", "bbbbbb"]);
        // the network's own report of an aircraft wins over the partner's
        assert_eq!(sky.entries[0].lat, Some(1.0));
        // the same two snapshots give the same merge
        assert!(Arc::ptr_eq(&sky, &p.fleet_sky(network.clone(), 30)));
        // a stale partner drops out
        p.set_snapshot(snap(now_s() - 120.0, &[r#"{"hex":"bbbbbb","lat":1.1,"lon":103.1}"#]));
        assert!(Arc::ptr_eq(&p.fleet_sky(network.clone(), 30), &network));
    }

    /// The rule in the source: partner data is named only where it is
    /// read (here and the fleet tier) and wired (state, main, settings).
    /// A new reader anywhere else fails this test, on purpose.
    #[test]
    fn only_the_fleet_tier_reads_partner_data() {
        let allowed = ["partner.rs", "fleet.rs", "state.rs", "main.rs", "settings.rs"];
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut stack = vec![dir];
        let mut offenders = vec![];
        while let Some(d) = stack.pop() {
            for f in std::fs::read_dir(&d).unwrap() {
                let path = f.unwrap().path();
                if path.is_dir() {
                    stack.push(path);
                    continue;
                }
                let name = path.file_name().unwrap().to_string_lossy().to_string();
                if !name.ends_with(".rs") || allowed.contains(&name.as_str()) {
                    continue;
                }
                let text = std::fs::read_to_string(&path).unwrap().to_lowercase();
                if text.contains("partner") {
                    offenders.push(path.display().to_string());
                }
            }
        }
        assert!(offenders.is_empty(), "partner data named outside the fleet tier: {offenders:?}");
    }
}
