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

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpStream;

use crate::sky::{parse_aircraft_full, Entry, LiveSky, Snapshot, SnapshotCell};
use crate::state::now_s;

/// How often the partner's snapshot is rebuilt while its lines flow.
const PUBLISH_S: f64 = 1.0;

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
