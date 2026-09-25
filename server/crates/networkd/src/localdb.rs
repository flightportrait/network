//! The instance's own state, for a server with no Postgres (a self-hosted
//! Pi): what its writers keep, in one SQLite file. The stations registry
//! (its feeders), the emergency squawks it heard, the estimate book's
//! memory and the NAT track messages. Same rules as the Postgres writers;
//! nothing here is shared with any other instance.
//!
//! Timestamps are stored as 'YYYY-MM-DD HH:MM:SS.ffffff' (UTC), which
//! sorts as time does.

use std::collections::HashMap;
use std::sync::Mutex;

use chrono::{DateTime, TimeDelta, Utc};
use rusqlite::{params, Connection, OptionalExtension};

const SCHEMA: &str = "
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    uuid_sha256 TEXT NOT NULL UNIQUE,
    half_id TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    coarse_lat REAL,
    coarse_lon REAL,
    label TEXT,
    msgs_per_s REAL,
    positions_per_s REAL,
    kbit_s REAL,
    rtt_ms REAL,
    positions_total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS station_sessions (
    id INTEGER PRIMARY KEY,
    station_id INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    ended_at TEXT,
    peak_msgs_per_s REAL NOT NULL DEFAULT 0,
    positions_total INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_station_sessions_station ON station_sessions (station_id, started_at);
CREATE TABLE IF NOT EXISTS squawk_events (
    id INTEGER PRIMARY KEY,
    hex TEXT NOT NULL,
    at TEXT NOT NULL,
    lat REAL,
    lon REAL,
    detail TEXT,
    visibility TEXT NOT NULL,
    UNIQUE (hex, at)
);
CREATE TABLE IF NOT EXISTS live_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nat_messages (
    id INTEGER PRIMARY KEY,
    issuer TEXT NOT NULL,
    tmi INTEGER,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    tracks TEXT NOT NULL,
    raw TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE (issuer, valid_from)
);
";

pub fn stamp(t: DateTime<Utc>) -> String {
    t.format("%Y-%m-%d %H:%M:%S%.6f").to_string()
}

fn parse(s: &str) -> DateTime<Utc> {
    crate::snap::ts(s).unwrap_or_default()
}

pub struct LocalDb {
    conn: Mutex<Connection>,
}

/// A station row as the routes read it: id, public id, half id, first
/// seen, last seen, positions total.
pub type StationRow = (i64, String, String, DateTime<Utc>, DateTime<Utc>, i64);
/// A roster row: public id, label, coarse lat, coarse lon, first seen,
/// last seen.
pub type RosterRow = (String, Option<String>, Option<f64>, Option<f64>, DateTime<Utc>, DateTime<Utc>);
/// A squawk this instance heard: at, lat, lon, detail (JSON text).
pub type OwnSquawk = (DateTime<Utc>, Option<f64>, Option<f64>, Option<String>);
/// A session: started, ended, peak messages per second, positions total.
pub type SessionRow = (DateTime<Utc>, Option<DateTime<Utc>>, f64, i64);

impl LocalDb {
    pub fn open(path: &str) -> rusqlite::Result<LocalDb> {
        if let Some(dir) = std::path::Path::new(path).parent().filter(|d| !d.as_os_str().is_empty()) {
            let _ = std::fs::create_dir_all(dir);
        }
        let c = Connection::open(path)?;
        c.execute_batch(SCHEMA)?;
        Ok(LocalDb { conn: Mutex::new(c) })
    }

    pub fn with<T>(&self, f: impl FnOnce(&mut Connection) -> rusqlite::Result<T>) -> rusqlite::Result<T> {
        f(&mut self.conn.lock().unwrap())
    }

    // ---- the stations registry ----------------------------------------------

    /// One clients.json poll, as the Postgres registry applies it.
    pub fn upsert_presence(
        &self,
        rows: &[(String, String, &crate::stations::Client)],
        now: DateTime<Utc>,
        reconnect_s: i64,
    ) -> rusqlite::Result<HashMap<String, crate::stations::Live>> {
        self.with(|c| {
            let tx = c.transaction()?;
            let mut presence = HashMap::new();
            let mut seen: Vec<i64> = vec![];
            let now_s = stamp(now);
            for (digest, half_id, row) in rows {
                let found: Option<i64> =
                    tx.query_row("SELECT id FROM stations WHERE uuid_sha256 = ?1", [digest], |r| r.get(0)).optional()?;
                let id = match found {
                    Some(id) => id,
                    None => {
                        tx.execute(
                            "INSERT INTO stations (public_id, uuid_sha256, half_id, first_seen, last_seen, positions_total) \
                             VALUES (?1, ?2, ?3, ?4, ?4, 0)",
                            params![format!("fp-{}", &digest[..10]), digest, half_id, now_s],
                        )?;
                        tx.last_insert_rowid()
                    }
                };
                tx.execute(
                    "UPDATE stations SET last_seen = ?2, msgs_per_s = ?3, positions_per_s = ?4, kbit_s = ?5, \
                     rtt_ms = ?6, positions_total = max(positions_total, ?7) WHERE id = ?1",
                    params![id, now_s, row.msgs_per_s, row.positions_per_s, row.kbit_s, row.rtt_ms, row.positions_total],
                )?;
                seen.push(id);
                let started = now - TimeDelta::microseconds((row.conn_time_s * 1e6).round_ties_even() as i64);
                let open: Option<(i64, String)> = tx
                    .query_row(
                        "SELECT id, started_at FROM station_sessions WHERE station_id = ?1 AND ended_at IS NULL \
                         ORDER BY started_at DESC LIMIT 1",
                        [id],
                        |r| Ok((r.get(0)?, r.get(1)?)),
                    )
                    .optional()?;
                let open = match open {
                    Some((sid, began)) if started > parse(&began) + TimeDelta::seconds(reconnect_s) => {
                        tx.execute("UPDATE station_sessions SET ended_at = last_seen_at WHERE id = ?1", [sid])?;
                        None
                    }
                    other => other.map(|o| o.0),
                };
                match open {
                    Some(sid) => {
                        tx.execute(
                            "UPDATE station_sessions SET last_seen_at = ?2, peak_msgs_per_s = max(peak_msgs_per_s, ?3), \
                             positions_total = max(positions_total, ?4) WHERE id = ?1",
                            params![sid, now_s, row.msgs_per_s, row.positions_total],
                        )?;
                    }
                    None => {
                        tx.execute(
                            "INSERT INTO station_sessions (station_id, started_at, last_seen_at, peak_msgs_per_s, positions_total) \
                             VALUES (?1, ?2, ?3, ?4, ?5)",
                            params![id, stamp(started), now_s, row.msgs_per_s.max(0.0), row.positions_total.max(0)],
                        )?;
                    }
                }
                presence.insert(
                    half_id.clone(),
                    crate::stations::Live {
                        kbit_s: row.kbit_s,
                        msgs_per_s: row.msgs_per_s,
                        positions_per_s: row.positions_per_s,
                        rtt_ms: row.rtt_ms,
                        connected_since: crate::stations::isoformat(started),
                    },
                );
            }
            tx.execute(
                "UPDATE station_sessions SET ended_at = last_seen_at WHERE ended_at IS NULL \
                 AND station_id NOT IN (SELECT value FROM json_each(?1))",
                [crate::snap::ids(&seen)],
            )?;
            tx.commit()?;
            Ok(presence)
        })
    }

    pub fn prune_sessions(&self, retention_days: i64, now: DateTime<Utc>) -> rusqlite::Result<usize> {
        let cutoff = stamp(now - TimeDelta::days(retention_days));
        self.with(|c| c.execute("DELETE FROM station_sessions WHERE ended_at IS NOT NULL AND ended_at < ?1", [cutoff]))
    }

    pub fn apply_receivers(&self, updates: &[(String, f64, f64)]) -> rusqlite::Result<()> {
        self.with(|c| {
            let tx = c.transaction()?;
            for (half_id, lat, lon) in updates {
                tx.execute("UPDATE stations SET coarse_lat = ?2, coarse_lon = ?3 WHERE half_id = ?1", params![half_id, lat, lon])?;
            }
            tx.commit()
        })
    }

    pub fn roster(&self) -> rusqlite::Result<Vec<RosterRow>> {
        self.with(|c| {
            let mut stmt = c.prepare_cached(
                "SELECT public_id, label, coarse_lat, coarse_lon, first_seen, last_seen FROM stations ORDER BY first_seen, id",
            )?;
            let rows = stmt.query_map([], |r| {
                let (a, b): (String, String) = (r.get(4)?, r.get(5)?);
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, parse(&a), parse(&b)))
            })?;
            rows.collect()
        })
    }

    pub fn station(&self, digest: &str) -> rusqlite::Result<Option<(StationRow, Vec<SessionRow>)>> {
        self.with(|c| {
            let s: Option<StationRow> = c
                .query_row(
                    "SELECT id, public_id, half_id, first_seen, last_seen, positions_total FROM stations WHERE uuid_sha256 = ?1",
                    [digest],
                    |r| {
                        let (a, b): (String, String) = (r.get(3)?, r.get(4)?);
                        Ok((r.get(0)?, r.get(1)?, r.get(2)?, parse(&a), parse(&b), r.get(5)?))
                    },
                )
                .optional()?;
            let Some(s) = s else { return Ok(None) };
            let mut stmt = c.prepare_cached(
                "SELECT started_at, ended_at, peak_msgs_per_s, positions_total FROM station_sessions WHERE station_id = ?1 ORDER BY id",
            )?;
            let sessions = stmt
                .query_map([s.0], |r| {
                    let (a, b): (String, Option<String>) = (r.get(0)?, r.get(1)?);
                    Ok((parse(&a), b.as_deref().map(parse), r.get(2)?, r.get(3)?))
                })?
                .collect::<rusqlite::Result<_>>()?;
            Ok(Some((s, sessions)))
        })
    }

    // ---- emergency squawks ------------------------------------------------------

    /// Keep each event once; how many were new.
    pub fn write_squawks(&self, events: &[crate::squawks::Event]) -> rusqlite::Result<usize> {
        self.with(|c| {
            let tx = c.transaction()?;
            let mut n = 0;
            for e in events {
                n += tx.execute(
                    "INSERT OR IGNORE INTO squawk_events (hex, at, lat, lon, detail, visibility) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    params![e.hex, stamp(e.timestamp()), e.lat, e.lon, e.detail_json(), e.visibility],
                )?;
            }
            tx.commit()?;
            Ok(n)
        })
    }

    /// The public events heard for one hex: at, lat, lon, detail.
    pub fn squawks_for(&self, hex: &str) -> rusqlite::Result<Vec<OwnSquawk>> {
        self.with(|c| {
            let mut stmt = c.prepare_cached(
                "SELECT at, lat, lon, detail FROM squawk_events WHERE hex = ?1 AND visibility = 'public' ORDER BY id",
            )?;
            let rows = stmt.query_map([hex], |r| {
                let at: String = r.get(0)?;
                Ok((parse(&at), r.get(1)?, r.get(2)?, r.get(3)?))
            })?;
            rows.collect()
        })
    }

    // ---- small state and NAT messages ----------------------------------------------

    pub fn get_state(&self, key: &str) -> rusqlite::Result<Option<String>> {
        self.with(|c| c.query_row("SELECT value FROM live_state WHERE key = ?1", [key], |r| r.get(0)).optional())
    }

    pub fn set_state(&self, key: &str, value: &str) -> rusqlite::Result<()> {
        self.with(|c| {
            c.execute(
                "INSERT INTO live_state (key, value, updated_at) VALUES (?1, ?2, ?3) \
                 ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                params![key, value, stamp(Utc::now())],
            )
            .map(|_| ())
        })
    }

    /// Keep a NAT message once (issuer + start of validity); true if new.
    #[allow(clippy::too_many_arguments)]
    pub fn store_nat(
        &self,
        issuer: &str,
        tmi: Option<i64>,
        from: DateTime<Utc>,
        to: DateTime<Utc>,
        tracks: &str,
        raw: Option<&str>,
    ) -> rusqlite::Result<bool> {
        self.with(|c| {
            let n = c.execute(
                "INSERT OR IGNORE INTO nat_messages (issuer, tmi, valid_from, valid_to, tracks, raw, fetched_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                params![issuer, tmi, stamp(from), stamp(to), tracks, raw, stamp(Utc::now())],
            )?;
            Ok(n > 0)
        })
    }
}
