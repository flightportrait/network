//! Emergency squawks as airframe events. Port of the Python service's
//! `squawks.py`, writing the same Postgres tables, so the airframe record
//! (which the nightly history import also maintains) stays one record.
//!
//! Every position line from our readsb passes through `observe`. An
//! aircraft squawking 7500, 7600 or 7700, or reporting a readsb emergency
//! state, becomes an event only once the signal holds: at least
//! MIN_REPORTS reports spanning MIN_HOLD_S seconds. One event per
//! episode; an episode ends after EPISODE_GAP_S without a signal. 7500
//! and "unlawful" are stored "held": served nowhere until reviewed, since
//! a mis-set 7500 published as a hijack is harmful misinformation.
//! Only our own sky: a point-mode instance records nothing.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::{DateTime, TimeZone, Utc};

use crate::pyjson::{write_str, Val};
use crate::sky::{Vals, F_ALT, F_FLIGHT, F_HEX, F_LAT, F_LON};
use crate::state::now_s;

const SOURCE: &str = "live";
const CODES: [&str; 3] = ["7500", "7600", "7700"];
const EMERGENCIES: [&str; 6] = ["general", "lifeguard", "minfuel", "nordo", "unlawful", "downed"];
const HELD: [&str; 2] = ["7500", "unlawful"];
const MIN_REPORTS: u32 = 2;
const MIN_HOLD_S: f64 = 20.0;
const EPISODE_GAP_S: f64 = 300.0;
const FLUSH_S: u64 = 15;

/// Squawk and emergency fields sit outside the live allowlist's index
/// constants; look them up by name.
fn field<'a>(vals: &'a Vals, name: &str) -> Option<&'a Val> {
    let i = crate::sky::FIELDS.iter().position(|f| *f == name)?;
    vals[i].as_ref()
}

fn str_of(v: Option<&Val>) -> Option<&str> {
    match v {
        Some(Val::Str(s)) => Some(s),
        _ => None,
    }
}

struct Episode {
    since: f64,
    last: f64,
    n: u32,
    recorded: bool,
    code: Option<String>,
    state: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Event {
    pub hex: String,
    pub at: f64,
    pub lat: Option<f64>,
    pub lon: Option<f64>,
    pub visibility: &'static str,
    pub code: Option<String>,
    pub emergency: Option<String>,
    pub callsign: Option<String>,
    pub alt_baro: Option<Val>,
}

impl Event {
    /// The event's `detail`, as the Python service's json.dumps writes it.
    fn detail_json(&self) -> String {
        let opt = |out: &mut String, v: &Option<String>| match v {
            Some(s) => write_str(out, s),
            None => out.push_str("null"),
        };
        let mut s = String::from("{\"code\": ");
        opt(&mut s, &self.code);
        s.push_str(", \"emergency\": ");
        opt(&mut s, &self.emergency);
        s.push_str(", \"callsign\": ");
        opt(&mut s, &self.callsign);
        s.push_str(", \"alt_baro\": ");
        match &self.alt_baro {
            Some(v) => v.write(&mut s),
            None => s.push_str("null"),
        }
        s.push('}');
        s
    }
}

#[derive(Default)]
pub struct Watcher {
    episodes: HashMap<String, Episode>,
    pending: Vec<Event>,
}

impl Watcher {
    pub fn observe(&mut self, vals: &Vals, now: f64) {
        let Some(hex) = str_of(vals[F_HEX].as_ref()).filter(|h| h.chars().count() == 6) else { return };
        let hex = hex.to_lowercase();
        let code = str_of(field(vals, "squawk")).filter(|s| CODES.contains(s)).map(str::to_string);
        let state = str_of(field(vals, "emergency")).filter(|s| EMERGENCIES.contains(s)).map(str::to_string);
        if code.is_none() && state.is_none() {
            return;
        }
        let fresh = self.episodes.get(&hex).is_none_or(|ep| now - ep.last > EPISODE_GAP_S);
        if fresh {
            self.episodes.insert(
                hex.clone(),
                Episode { since: now, last: now, n: 0, recorded: false, code: code.clone(), state: state.clone() },
            );
        }
        let ep = self.episodes.get_mut(&hex).unwrap();
        ep.last = now;
        ep.n += 1;
        if ep.code.is_none() {
            ep.code = code;
        }
        if ep.state.is_none() {
            ep.state = state;
        }
        if !ep.recorded && ep.n >= MIN_REPORTS && now - ep.since >= MIN_HOLD_S {
            ep.recorded = true;
            let held = ep.code.as_deref().is_some_and(|c| HELD.contains(&c))
                || ep.state.as_deref().is_some_and(|s| HELD.contains(&s));
            let callsign = str_of(vals[F_FLIGHT].as_ref()).map(str::trim).filter(|s| !s.is_empty()).map(str::to_string);
            self.pending.push(Event {
                hex,
                at: ep.since,
                lat: vals[F_LAT].as_ref().and_then(Val::as_f64),
                lon: vals[F_LON].as_ref().and_then(Val::as_f64),
                visibility: if held { "held" } else { "public" },
                code: ep.code.clone(),
                emergency: ep.state.clone(),
                callsign,
                alt_baro: vals[F_ALT].clone().filter(|v| !v.is_null()),
            });
        }
    }

    /// Forget episodes that ended.
    pub fn sweep(&mut self, now: f64) {
        self.episodes.retain(|_, ep| now - ep.last <= EPISODE_GAP_S);
    }

    pub fn drain(&mut self) -> Vec<Event> {
        std::mem::take(&mut self.pending)
    }

    pub fn requeue(&mut self, events: Vec<Event>) {
        let later = std::mem::take(&mut self.pending);
        self.pending = events;
        self.pending.extend(later);
    }
}

fn timestamp(at: f64) -> DateTime<Utc> {
    // microseconds, as a Python datetime holds them
    let micros = (at * 1e6).round() as i64;
    Utc.timestamp_micros(micros).single().unwrap_or_else(Utc::now)
}

/// Write events, each attached to the airframe the record holds for its
/// hex (a new airframe with a live hex spell when there is none).
/// Idempotent: an event already recorded is skipped. Returns how many
/// were written.
pub async fn write_events(db: &tokio_postgres::Client, events: &[Event]) -> Result<usize, tokio_postgres::Error> {
    let mut written = 0;
    db.batch_execute("BEGIN").await?;
    let result = async {
        for ev in events {
            let at = timestamp(ev.at);
            let day = at.date_naive();
            let found = db
                .query_opt(
                    "SELECT airframe_id FROM airframe_spells WHERE kind = 'hex' AND value = $1 \
                     ORDER BY first_date DESC LIMIT 1",
                    &[&ev.hex],
                )
                .await?;
            let airframe_id: i64 = match found {
                Some(r) => r.get(0),
                None => {
                    let now = Utc::now();
                    let id: i64 = db
                        .query_one(
                            "INSERT INTO airframes (first_observed, last_observed, created_at, updated_at) \
                             VALUES ($1, $1, $2, $2) RETURNING id",
                            &[&day, &now],
                        )
                        .await?
                        .get(0);
                    db.execute(
                        "INSERT INTO airframe_spells (airframe_id, kind, value, first_date, last_date, source) \
                         VALUES ($1, 'hex', $2, $3, $3, $4)",
                        &[&id, &ev.hex, &day, &SOURCE],
                    )
                    .await?;
                    id
                }
            };
            let exists = db
                .query_opt(
                    "SELECT id FROM airframe_events WHERE airframe_id = $1 AND kind = 'squawk' AND at = $2 AND source = $3",
                    &[&airframe_id, &at, &SOURCE],
                )
                .await?;
            if exists.is_some() {
                continue;
            }
            db.execute(
                "INSERT INTO airframe_events (airframe_id, kind, at, lat, lon, detail, source, visibility) \
                 VALUES ($1, 'squawk', $2, $3, $4, $5::text::json, $6, $7)",
                &[&airframe_id, &at, &ev.lat, &ev.lon, &ev.detail_json(), &SOURCE, &ev.visibility],
            )
            .await?;
            written += 1;
        }
        Ok::<_, tokio_postgres::Error>(())
    }
    .await;
    match result {
        Ok(()) => {
            db.batch_execute("COMMIT").await?;
            Ok(written)
        }
        Err(e) => {
            let _ = db.batch_execute("ROLLBACK").await;
            Err(e)
        }
    }
}

/// Write recorded episodes every FLUSH_S; a failed write keeps them for
/// the next round, and a lost connection is reopened.
pub async fn flush_loop(watcher: Arc<Mutex<Watcher>>, database_url: String) {
    let mut db: Option<tokio_postgres::Client> = None;
    loop {
        tokio::time::sleep(Duration::from_secs(FLUSH_S)).await;
        let events = {
            let mut w = watcher.lock().unwrap();
            w.sweep(now_s());
            w.drain()
        };
        if events.is_empty() {
            continue;
        }
        if db.as_ref().is_none_or(|c| c.is_closed()) {
            db = match crate::pg::connect(&database_url).await {
                Ok(c) => Some(c),
                Err(e) => {
                    eprintln!("squawks: database: {e}");
                    watcher.lock().unwrap().requeue(events);
                    continue;
                }
            };
        }
        match write_events(db.as_ref().unwrap(), &events).await {
            Ok(n) if n > 0 => eprintln!("squawks: recorded {n} event(s)"),
            Ok(_) => {}
            Err(e) => {
                eprintln!("squawks: write failed, retrying: {e}");
                watcher.lock().unwrap().requeue(events);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sky::parse_aircraft;

    fn line(code: &str, hex: &str, extra: &str) -> Vals {
        parse_aircraft(&format!(
            r#"{{"hex":"{hex}","squawk":"{code}","flight":"TVS2221 ","lat":50.1,"lon":14.2,"alt_baro":35000{extra}}}"#
        ))
        .unwrap()
    }

    #[test]
    fn a_signal_counts_only_once_it_holds() {
        let mut w = Watcher::default();
        w.observe(&line("7700", "49d283", ""), 1000.0);
        w.observe(&line("7700", "49d283", ""), 1010.0); // two reports, 10 s apart
        assert!(w.drain().is_empty());
        w.observe(&line("7700", "49d283", ""), 1021.0); // held 21 s
        let ev = w.drain();
        assert_eq!(ev.len(), 1);
        assert_eq!((ev[0].at, ev[0].visibility), (1000.0, "public"));
        assert_eq!(ev[0].detail_json(), r#"{"code": "7700", "emergency": null, "callsign": "TVS2221", "alt_baro": 35000}"#);
        w.observe(&line("7700", "49d283", ""), 1100.0); // same episode
        assert!(w.drain().is_empty());
    }

    #[test]
    fn one_frame_and_ordinary_codes_are_nothing() {
        let mut w = Watcher::default();
        w.observe(&line("7700", "49d283", ""), 0.0);
        w.observe(&line("1000", "49d283", ""), 30.0);
        w.observe(&line("2000", "49d283", r#","emergency":"none""#), 60.0);
        assert!(w.drain().is_empty());
    }

    #[test]
    fn a_new_episode_after_silence_and_7500_is_held() {
        let mut w = Watcher::default();
        for t in [0.0, 25.0] {
            w.observe(&line("7600", "49d283", ""), t);
        }
        for t in [1000.0, 1030.0] {
            w.observe(&line("7500", "49d283", ""), t);
        }
        for t in [0.0, 25.0] {
            w.observe(&line("2000", "4ca123", r#","emergency":"unlawful""#), t);
        }
        let got: Vec<(Option<String>, &str)> = w.drain().into_iter().map(|e| (e.code, e.visibility)).collect();
        assert_eq!(
            got,
            [(Some("7600".into()), "public"), (Some("7500".into()), "held"), (None, "held")]
        );
    }

    #[test]
    fn microsecond_timestamps() {
        assert_eq!(timestamp(1790000000.25).timestamp_micros(), 1790000000250000);
    }
}

#[cfg(test)]
mod db_tests {
    use super::*;
    use crate::sky::parse_aircraft;

    /// Against a real database: SQUAWK_TEST_DB=postgresql://... (skipped
    /// without it). Writes the Python test's scenario twice.
    #[tokio::test]
    async fn writes_like_the_python_service() {
        let Ok(url) = std::env::var("SQUAWK_TEST_DB") else { return };
        let db = crate::pg::connect(&url).await.unwrap();
        let mut w = Watcher::default();
        let l = |code: &str, hex: &str, extra: &str| {
            parse_aircraft(&format!(
                r#"{{"hex":"{hex}","squawk":"{code}","flight":"TVS2221 ","lat":50.1,"lon":14.2,"alt_baro":35000{extra}}}"#
            ))
            .unwrap()
        };
        for t in [1790000000.0, 1790000030.0] {
            w.observe(&l("7700", "49d283", ""), t);
        }
        for t in [1790000000.5, 1790000026.0] {
            w.observe(&l("2000", "4ca123", r#","emergency":"unlawful""#), t);
        }
        let events = w.drain();
        assert_eq!(write_events(&db, &events).await.unwrap(), 2);
        assert_eq!(write_events(&db, &events).await.unwrap(), 0);
    }
}
