//! The live sky: readsb's JSON position port merged into a state, a
//! snapshot published from it at most four times a second, and the
//! aircraft.json poll as the fallback while lines do not flow.
//!
//! Semantics follow the Python service (`livesky.py`, `poller.py`,
//! `snapshot.py`): the same freshness, warm-up, expiry, seeding and
//! field allowlist, and aircraft keep the order they were first heard in.

use std::sync::{Arc, OnceLock, RwLock};
use std::time::Duration;

use bytes::Bytes;
use indexmap::IndexMap;
use serde::de::{Deserialize, Deserializer};
use serde_json::value::RawValue;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::TcpStream;

use crate::pyjson::{round_to, write_float, write_str, Obj, Val};
use crate::state::{now_s, App};

pub const FRESH_S: f64 = 10.0;
pub const WARMUP_S: f64 = 3.0;
pub const EXPIRE_S: f64 = 60.0;
pub const PUBLISH_MIN_S: f64 = 0.25;
pub const TRACE_EVERY_S: f64 = 2.0;

/// The passthrough allowlist, in output order.
pub const FIELDS: [&str; 15] = [
    "hex", "flight", "t", "r", "lat", "lon", "alt_baro", "gs",
    "track", "category", "squawk", "seen", "seen_pos",
    "baro_rate", "emergency",
];
pub const NF: usize = FIELDS.len();
pub const F_HEX: usize = 0;
pub const F_FLIGHT: usize = 1;
pub const F_LAT: usize = 4;
pub const F_LON: usize = 5;
pub const F_ALT: usize = 6;
pub const F_TRACK: usize = 8;
pub const F_SEEN: usize = 11;
pub const F_SEEN_POS: usize = 12;

pub type Vals = [Option<Val>; NF];

/// A field that may be present with any JSON value, `null` included.
#[derive(Default)]
struct Present<'a>(Option<&'a RawValue>);

impl<'de: 'a, 'a> Deserialize<'de> for Present<'a> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        <&RawValue>::deserialize(d).map(|r| Present(Some(r)))
    }
}

/// One readsb aircraft object, only the allowlisted fields captured.
#[derive(serde::Deserialize)]
struct Fields<'a> {
    #[serde(default, borrow)]
    hex: Present<'a>,
    #[serde(default, borrow)]
    flight: Present<'a>,
    #[serde(default, borrow)]
    t: Present<'a>,
    #[serde(default, borrow)]
    r: Present<'a>,
    #[serde(default, borrow)]
    lat: Present<'a>,
    #[serde(default, borrow)]
    lon: Present<'a>,
    #[serde(default, borrow)]
    alt_baro: Present<'a>,
    #[serde(default, borrow)]
    gs: Present<'a>,
    #[serde(default, borrow)]
    track: Present<'a>,
    #[serde(default, borrow)]
    category: Present<'a>,
    #[serde(default, borrow)]
    squawk: Present<'a>,
    #[serde(default, borrow)]
    seen: Present<'a>,
    #[serde(default, borrow)]
    seen_pos: Present<'a>,
    #[serde(default, borrow)]
    baro_rate: Present<'a>,
    #[serde(default, borrow)]
    emergency: Present<'a>,
    /// readsb's message source (`adsb_icao`, `mlat`, ...): kept, not served
    #[serde(default, borrow, rename = "type")]
    kind: Present<'a>,
    /// readsb's database flags, when it loads one: kept, not served
    #[serde(default, borrow, rename = "dbFlags")]
    db_flags: Present<'a>,
}

/// What readsb says about an aircraft that the public allowlist leaves
/// out. Never written into `Entry::json`; only the private fleet tier
/// reads it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Hidden {
    /// the message source, collapsed: adsb, mlat, tisb, adsr, adsc,
    /// mode_s or other
    pub source: Option<&'static str>,
    /// readsb's dbFlags (bit 0: military)
    pub db_flags: Option<i64>,
}

/// readsb's `type` -> the source the fleet tier reports.
pub fn source_of(kind: &str) -> &'static str {
    match kind {
        "mlat" => "mlat",
        "mode_s" => "mode_s",
        "adsc" => "adsc",
        k if k.starts_with("adsb") => "adsb",
        k if k.starts_with("tisb") => "tisb",
        k if k.starts_with("adsr") => "adsr",
        _ => "other",
    }
}

impl Fields<'_> {
    fn into_vals(self) -> (Vals, Hidden) {
        let hidden = Hidden {
            source: self.kind.0.and_then(|r| serde_json::from_str::<&str>(r.get()).ok()).map(source_of),
            db_flags: self.db_flags.0.and_then(|r| r.get().parse::<i64>().ok()),
        };
        let raw = [
            self.hex, self.flight, self.t, self.r, self.lat, self.lon, self.alt_baro, self.gs,
            self.track, self.category, self.squawk, self.seen, self.seen_pos, self.baro_rate,
            self.emergency,
        ];
        let mut vals: Vals = Default::default();
        for (slot, p) in vals.iter_mut().zip(raw) {
            *slot = p.0.map(|r| Val::from_raw(r.get()));
        }
        // the callsign arrives space-padded
        if let Some(Val::Str(s)) = &vals[F_FLIGHT] {
            let t = s.trim();
            if t.len() != s.len() {
                vals[F_FLIGHT] = Some(Val::Str(t.into()));
            }
        }
        (vals, hidden)
    }
}

/// Parse one aircraft object into its allowlisted values; None when it
/// is not an object.
#[cfg(test)]
pub fn parse_aircraft(json: &str) -> Option<Vals> {
    parse_aircraft_full(json).map(|(v, _)| v)
}

/// `parse_aircraft`, plus what the allowlist leaves out.
pub fn parse_aircraft_full(json: &str) -> Option<(Vals, Hidden)> {
    serde_json::from_str::<Fields>(json).ok().map(Fields::into_vals)
}

// ---- the snapshot -------------------------------------------------------

pub struct Entry {
    /// Shared with every stream that remembers this aircraft.
    pub hex: Arc<str>,
    pub vals: Box<Vals>,
    /// The entry as served, `{...}`.
    pub json: Box<str>,
    /// Identity of every field but `seen`/`seen_pos`: equal signatures
    /// mean the stream has nothing new to say about this aircraft.
    pub sig: u64,
    pub lat: Option<f64>,
    pub lon: Option<f64>,
    pub has_pos: bool,
    /// Where each field's `"key":value` sits in `json` (empty: absent),
    /// so a stream can send the fields that changed without re-encoding.
    pub spans: [(u32, u32); NF],
    /// Kept beside the served fields, never in `json`.
    pub hidden: Hidden,
}

fn fnv(h: u64, bytes: &[u8]) -> u64 {
    let mut h = h;
    for b in bytes {
        h ^= *b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

impl Entry {
    pub fn new(vals: Vals) -> Entry {
        Entry::with_hidden(vals, Hidden::default())
    }

    pub fn with_hidden(vals: Vals, hidden: Hidden) -> Entry {
        let mut json = String::with_capacity(256);
        let mut sig: u64 = 0xcbf29ce484222325;
        json.push('{');
        let mut first = true;
        let mut spans = [(0u32, 0u32); NF];
        for (i, v) in vals.iter().enumerate() {
            let Some(v) = v else { continue };
            if !first {
                json.push(',');
            }
            first = false;
            let start = json.len();
            write_str(&mut json, FIELDS[i]);
            json.push(':');
            v.write(&mut json);
            spans[i] = (start as u32, json.len() as u32);
            if i != F_SEEN && i != F_SEEN_POS && !v.is_null() {
                sig = fnv(sig, &json.as_bytes()[start..]);
                sig = fnv(sig, b"\x00");
            }
        }
        json.push('}');
        let hex: Arc<str> = match &vals[F_HEX] {
            Some(Val::Str(s)) => Arc::from(&**s),
            _ => Arc::from(""),
        };
        let lat = vals[F_LAT].as_ref().and_then(Val::as_f64);
        let lon = vals[F_LON].as_ref().and_then(Val::as_f64);
        let present = |i: usize| vals[i].as_ref().is_some_and(|v| !v.is_null());
        let has_pos = present(F_LAT) && present(F_LON);
        Entry { hex, vals: Box::new(vals), json: json.into(), sig, lat, lon, has_pos, spans, hidden }
    }

    /// Field `i` as `"key":value`, or None when absent.
    pub fn field(&self, i: usize) -> Option<&str> {
        let (a, b) = self.spans[i];
        (b > a).then(|| &self.json[a as usize..b as usize])
    }

    pub fn in_bbox(&self, b: &BBox) -> bool {
        let (Some(lat), Some(lon)) = (self.lat, self.lon) else { return false };
        if !(b.s <= lat && lat <= b.n) {
            return false;
        }
        if b.w <= b.e {
            b.w <= lon && lon <= b.e
        } else {
            lon >= b.w || lon <= b.e
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct BBox {
    pub w: f64,
    pub s: f64,
    pub e: f64,
    pub n: f64,
}

#[derive(Default)]
pub struct Snapshot {
    pub generated_at: f64,
    pub entries: Vec<Entry>,
    pub with_pos: usize,
    body: OnceLock<Bytes>,
    grid: OnceLock<Grid>,
    by_hex: OnceLock<std::collections::HashMap<Arc<str>, u32>>,
}

/// Entries with a position, bucketed into 2° cells (compressed rows:
/// `start[c]..start[c+1]` indexes `items`), so a box visits only the
/// cells it overlaps instead of the whole sky.
struct Grid {
    start: Vec<u32>,
    items: Vec<u32>,
}

const CELL_DEG: f64 = 2.0;
const GRID_W: usize = (360.0 / CELL_DEG) as usize;
const GRID_H: usize = (180.0 / CELL_DEG) as usize;

fn cell_x(lon: f64) -> usize {
    (((lon + 180.0) / CELL_DEG).floor().max(0.0) as usize).min(GRID_W - 1)
}

fn cell_y(lat: f64) -> usize {
    (((lat + 90.0) / CELL_DEG).floor().max(0.0) as usize).min(GRID_H - 1)
}

impl Grid {
    fn build(entries: &[Entry]) -> Grid {
        let mut count = vec![0u32; GRID_W * GRID_H + 1];
        let mut cells = Vec::with_capacity(entries.len());
        for (i, e) in entries.iter().enumerate() {
            if let (Some(lat), Some(lon)) = (e.lat, e.lon) {
                if lat.is_finite() && lon.is_finite() {
                    let c = cell_y(lat) * GRID_W + cell_x(lon);
                    count[c + 1] += 1;
                    cells.push((c, i as u32));
                }
            }
        }
        for c in 1..count.len() {
            count[c] += count[c - 1];
        }
        let mut fill = count.clone();
        let mut items = vec![0u32; cells.len()];
        // entries go in snapshot order within each cell
        for (c, i) in cells {
            items[fill[c] as usize] = i;
            fill[c] += 1;
        }
        Grid { start: count, items }
    }

    /// Indexes of the entries a box may hold, in snapshot order. A
    /// superset: cells on the edge still need `in_bbox`.
    fn candidates(&self, b: &BBox, out: &mut Vec<u32>) {
        out.clear();
        let (y0, y1) = (cell_y(b.s), cell_y(b.n));
        let xs: Vec<(usize, usize)> = if b.w <= b.e {
            vec![(cell_x(b.w), cell_x(b.e))]
        } else {
            vec![(cell_x(b.w), GRID_W - 1), (0, cell_x(b.e))]
        };
        for y in y0..=y1 {
            for &(x0, x1) in &xs {
                let (a, z) = (self.start[y * GRID_W + x0], self.start[y * GRID_W + x1 + 1]);
                out.extend_from_slice(&self.items[a as usize..z as usize]);
            }
        }
        out.sort_unstable();
        // a wrapped box whose edges share a column visits it twice
        out.dedup();
    }
}

impl Snapshot {
    pub fn new(generated_at: f64, entries: Vec<Entry>) -> Snapshot {
        let with_pos = entries.iter().filter(|e| e.has_pos).count();
        Snapshot {
            generated_at,
            entries,
            with_pos,
            body: OnceLock::new(),
            grid: OnceLock::new(),
            by_hex: OnceLock::new(),
        }
    }

    pub fn count(&self) -> usize {
        self.entries.len()
    }

    pub fn fresh(&self, stale_after_s: i64) -> bool {
        self.generated_at > 0.0 && now_s() - self.generated_at <= stale_after_s as f64
    }

    /// An aircraft by hex (the first, if a polled file repeats one).
    pub fn find(&self, hex: &str) -> Option<&Entry> {
        let map = self.by_hex.get_or_init(|| {
            let mut m = std::collections::HashMap::with_capacity(self.entries.len());
            for (i, e) in self.entries.iter().enumerate() {
                m.entry(e.hex.clone()).or_insert(i as u32);
            }
            m
        });
        map.get(hex).map(|&i| &self.entries[i as usize])
    }

    /// The entries inside `b`, in snapshot order, into `out` (indexes).
    pub fn in_box(&self, b: &BBox, out: &mut Vec<u32>) {
        let grid = self.grid.get_or_init(|| Grid::build(&self.entries));
        grid.candidates(b, out);
        out.retain(|&i| self.entries[i as usize].in_bbox(b));
    }

    /// The `/v1/aircraft` body for the whole sky, encoded once.
    pub fn body(&self) -> Bytes {
        self.body.get_or_init(|| Bytes::from(self.render(None))).clone()
    }

    pub fn render(&self, bbox: Option<&BBox>) -> String {
        let size: usize = self.entries.iter().map(|e| e.json.len() + 1).sum();
        let mut out = String::with_capacity(size + 128);
        let mut o = Obj::new(&mut out);
        o.f64("generated_at", self.generated_at)
            .int("total", self.count() as i64)
            .int("with_position", self.with_pos as i64);
        let buf = o.key("aircraft");
        buf.push('[');
        let mut first = true;
        let mut push = |e: &Entry| {
            if !first {
                buf.push(',');
            }
            first = false;
            buf.push_str(&e.json);
        };
        match bbox {
            None => self.entries.iter().for_each(&mut push),
            Some(b) => {
                let mut idx = Vec::new();
                self.in_box(b, &mut idx);
                idx.iter().for_each(|&i| push(&self.entries[i as usize]));
            }
        }
        buf.push(']');
        o.end();
        out
    }
}

// ---- the pushed sky -----------------------------------------------------

struct Item {
    vals: Vals,
    hidden: Hidden,
    at: f64,
}

pub struct LiveSky {
    max_aircraft: usize,
    aircraft: IndexMap<Box<str>, Item>,
    pub version: u64,
    pub last_line_at: f64,
    pub connected: bool,
    pub connected_at: f64,
    pub seeded: bool,
}

impl LiveSky {
    pub fn new(max_aircraft: usize) -> LiveSky {
        LiveSky {
            max_aircraft,
            aircraft: IndexMap::new(),
            version: 0,
            last_line_at: 0.0,
            connected: false,
            connected_at: 0.0,
            seeded: false,
        }
    }

    /// Lines are flowing and have been for the warm-up.
    pub fn fresh(&self, now: f64) -> bool {
        self.last_line_at > 0.0
            && now - self.last_line_at <= FRESH_S
            && self.connected_at > 0.0
            && now - self.connected_at >= WARMUP_S
    }

    pub fn ingest(&mut self, vals: Vals, now: f64) -> bool {
        self.ingest_full(vals, Hidden::default(), now)
    }

    pub fn ingest_full(&mut self, vals: Vals, hidden: Hidden, now: f64) -> bool {
        let hex = match &vals[F_HEX] {
            Some(Val::Str(s)) if !s.is_empty() => s.clone(),
            _ => return false,
        };
        self.version += 1;
        // an existing key keeps its place: first-heard order
        self.aircraft.insert(hex, Item { vals, hidden, at: now });
        self.last_line_at = now;
        true
    }

    /// Take the aircraft of `snap` the state does not know yet.
    pub fn seed(&mut self, snap: &Snapshot, now: f64) -> usize {
        let mut taken = 0;
        for e in &snap.entries {
            if e.hex.is_empty() || self.aircraft.contains_key(&*e.hex) {
                continue;
            }
            self.aircraft.insert(Box::from(&*e.hex), Item { vals: (*e.vals).clone(), hidden: e.hidden.clone(), at: now });
            taken += 1;
        }
        if taken > 0 {
            self.version += 1;
        }
        taken
    }

    /// The state as a snapshot: `seen`/`seen_pos` aged to now, aircraft
    /// not heard for EXPIRE_S dropped.
    pub fn snapshot(&mut self, now: f64) -> Snapshot {
        let mut out = Vec::with_capacity(self.aircraft.len().min(self.max_aircraft));
        let mut expired = Vec::new();
        for (i, item) in self.aircraft.values().enumerate() {
            let age = now - item.at;
            let seen = item.vals[F_SEEN].as_ref().and_then(Val::as_f64).unwrap_or(0.0);
            if seen + age > EXPIRE_S {
                expired.push(i);
                continue;
            }
            let mut vals = item.vals.clone();
            for f in [F_SEEN, F_SEEN_POS] {
                if let Some(x) = vals[f].as_ref().and_then(Val::as_f64) {
                    vals[f] = Some(Val::Float(round_to(x + age, 1)));
                }
            }
            out.push(Entry::with_hidden(vals, item.hidden.clone()));
            if out.len() >= self.max_aircraft {
                break;
            }
        }
        if !expired.is_empty() {
            let mut k = 0;
            let mut next = expired.iter().peekable();
            self.aircraft.retain(|_, _| {
                let drop = next.peek() == Some(&&k);
                if drop {
                    next.next();
                }
                k += 1;
                !drop
            });
        }
        Snapshot::new(self.last_line_at, out)
    }
}

// ---- tasks --------------------------------------------------------------

/// Keep one connection to readsb's JSON port; merge every line.
pub async fn read_lines(app: Arc<App>, endpoint: String) {
    let mut backoff = 1.0f64;
    loop {
        match tokio::time::timeout(Duration::from_secs(10), TcpStream::connect(&endpoint)).await {
            Ok(Ok(sock)) => {
                {
                    let mut live = app.live.lock().unwrap();
                    live.connected = true;
                    live.connected_at = now_s();
                    live.seeded = false;
                }
                backoff = 1.0;
                eprintln!("live sky connected to {endpoint}");
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
                    let now = now_s();
                    if let Some(w) = &app.squawks {
                        w.lock().unwrap().observe(&vals, now);
                    }
                    app.live.lock().unwrap().ingest_full(vals, hidden, now);
                }
                app.live.lock().unwrap().connected = false;
            }
            Ok(Err(e)) => eprintln!("live sky {endpoint}: {e}"),
            Err(_) => eprintln!("live sky {endpoint}: connect timed out"),
        }
        eprintln!("live sky reconnecting in {backoff:.0} s");
        tokio::time::sleep(Duration::from_secs_f64(backoff)).await;
        backoff = (backoff * 2.0).min(30.0);
    }
}

/// While lines flow: rebuild the snapshot on change, trail points every
/// couple of seconds, wake the sockets.
pub async fn publish(app: Arc<App>) {
    let mut seen_version = 0;
    let mut last_trace = 0.0;
    let mut tick = tokio::time::interval(Duration::from_secs_f64(PUBLISH_MIN_S));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tick.tick().await;
        let now = now_s();
        let snap = {
            let mut live = app.live.lock().unwrap();
            if live.version == seen_version || !live.fresh(now) {
                continue;
            }
            if !live.seeded {
                let current = app.snapshot();
                live.seed(&current, now);
                live.seeded = true;
            }
            seen_version = live.version;
            Arc::new(live.snapshot(now))
        };
        app.set_snapshot(snap.clone());
        if now - last_trace >= TRACE_EVERY_S {
            last_trace = now;
            app.traces.lock().unwrap().record(&snap);
            if let Some(est) = &app.estimates {
                est.observe(snap.entries.iter().map(|e| &*e.vals), now);
            }
        }
        app.bump();
    }
}

#[derive(serde::Deserialize)]
struct PollBody<'a> {
    #[serde(default)]
    now: Option<serde_json::Value>,
    #[serde(default, borrow)]
    aircraft: Option<Vec<&'a RawValue>>,
    #[serde(default, borrow)]
    ac: Option<Vec<&'a RawValue>>,
}

/// aircraft.json (or a v2 point answer) -> snapshot, as build_snapshot.
pub fn snapshot_from_poll(body: &[u8], max_aircraft: usize, point: bool) -> anyhow::Result<Snapshot> {
    let raw: PollBody = serde_json::from_slice(body)?;
    let mut now = raw.now.as_ref().and_then(|v| v.as_f64()).filter(|v| *v != 0.0);
    let list = if point {
        now = now.map(|n| if n > 1e12 { n / 1000.0 } else { n });
        raw.ac.filter(|a| !a.is_empty()).or(raw.aircraft)
    } else {
        raw.aircraft
    };
    let entries = list
        .unwrap_or_default()
        .into_iter()
        .take(max_aircraft)
        .filter_map(|r| parse_aircraft_full(r.get()))
        .map(|(v, h)| Entry::with_hidden(v, h))
        .collect();
    Ok(Snapshot::new(now.unwrap_or_else(now_s), entries))
}

pub async fn poll_snapshot_once(app: &App) -> anyhow::Result<()> {
    if app.live.lock().unwrap().fresh(now_s()) {
        return Ok(()); // the pushed sky is speaking; the poll waits
    }
    let s = &app.settings;
    let snap = if s.point_mode() {
        let url = s
            .source_point_url
            .replace("{lat}", &py_num(s.source_lat))
            .replace("{lon}", &py_num(s.source_lon))
            .replace("{radius}", &s.source_radius_nm.to_string());
        let body = app.upstream.get_url(&url).await?;
        snapshot_from_poll(&body, s.max_aircraft, true)?
    } else {
        let body = app.upstream.get("/data/aircraft.json").await?;
        let snap = snapshot_from_poll(&body, s.max_aircraft, false)?;
        if let Some(w) = &app.squawks {
            let now = now_s();
            let mut w = w.lock().unwrap();
            for e in &snap.entries {
                w.observe(&e.vals, now);
            }
        }
        snap
    };
    let snap = Arc::new(snap);
    app.set_snapshot(snap.clone());
    app.traces.lock().unwrap().record(&snap);
    if let Some(est) = &app.estimates {
        est.observe(snap.entries.iter().map(|e| &*e.vals), now_s());
    }
    app.bump();
    Ok(())
}

fn py_num(x: f64) -> String {
    let mut s = String::new();
    write_float(&mut s, x);
    s
}

/// A snapshot slot readers take without blocking the writer for long.
pub struct SnapshotCell(RwLock<Arc<Snapshot>>);

impl SnapshotCell {
    pub fn new() -> SnapshotCell {
        SnapshotCell(RwLock::new(Arc::new(Snapshot::default())))
    }
    pub fn get(&self) -> Arc<Snapshot> {
        self.0.read().unwrap().clone()
    }
    pub fn set(&self, s: Arc<Snapshot>) {
        *self.0.write().unwrap() = s;
    }
}

impl Default for SnapshotCell {
    fn default() -> Self {
        Self::new()
    }
}


#[cfg(test)]
mod tests {
    use super::*;

    fn vals(json: &str) -> Vals {
        parse_aircraft(json).unwrap()
    }

    #[test]
    fn lines_merge_and_age() {
        let mut live = LiveSky::new(10);
        let t0 = 1000.0;
        assert!(live.ingest(vals(r#"{"hex":"aaaaaa","flight":"SIA1  ","lat":1.3,"lon":103.8,"seen":0.2,"seen_pos":0.2,"rssi":-9.0}"#), t0));
        assert!(parse_aircraft(r#"{"flight":"nohex"}"#).is_some_and(|v| !live.ingest(v, t0)));
        assert!(live.ingest(vals(r#"{"hex":"aaaaaa","lat":1.31,"lon":103.81,"seen":0.0,"seen_pos":0.0}"#), t0 + 3.0));
        let snap = live.snapshot(t0 + 5.0);
        assert_eq!((snap.count(), snap.with_pos), (1, 1));
        assert_eq!(&*snap.entries[0].json, r#"{"hex":"aaaaaa","lat":1.31,"lon":103.81,"seen":2.0,"seen_pos":2.0}"#);
        assert_eq!(snap.generated_at, t0 + 3.0);
        assert!(!live.fresh(t0 + 3.0), "not before the warm-up");
        live.connected_at = t0 - WARMUP_S;
        assert!(live.fresh(t0 + 3.0 + FRESH_S) && !live.fresh(t0 + 3.0 + FRESH_S + 1.0));
        assert_eq!(live.snapshot(t0 + 3.0 + 61.0).count(), 0);
    }

    #[test]
    fn order_is_first_heard_and_flight_trimmed() {
        let mut live = LiveSky::new(10);
        live.ingest(vals(r#"{"hex":"b","flight":"AB12   "}"#), 1.0);
        live.ingest(vals(r#"{"hex":"a"}"#), 1.0);
        live.ingest(vals(r#"{"hex":"b","flight":"AB12   ","lat":null}"#), 2.0);
        let snap = live.snapshot(2.0);
        let hexes: Vec<&str> = snap.entries.iter().map(|e| &*e.hex).collect();
        assert_eq!(hexes, ["b", "a"]);
        assert_eq!(&*snap.entries[0].json, r#"{"hex":"b","flight":"AB12","lat":null}"#);
        assert!(!snap.entries[0].has_pos);
    }

    #[test]
    fn signature_ignores_seen_and_nulls() {
        let a = Entry::new(vals(r#"{"hex":"a","lat":1.0,"seen":0.1}"#));
        let b = Entry::new(vals(r#"{"hex":"a","lat":1.0,"seen":3.1,"gs":null}"#));
        let c = Entry::new(vals(r#"{"hex":"a","lat":1.5,"seen":0.1}"#));
        assert_eq!(a.sig, b.sig);
        assert_ne!(a.sig, c.sig);
    }

    #[test]
    fn poll_body_like_build_snapshot() {
        let body = br#"{"now":1700000000.5,"aircraft":[{"hex":"abc123","lat":1,"lon":2,"rssi":-3},7,{"hex":"def456"}]}"#;
        let s = snapshot_from_poll(body, 10, false).unwrap();
        assert_eq!(s.generated_at, 1700000000.5);
        assert_eq!(s.count(), 2);
        assert_eq!(s.render(None), r#"{"generated_at":1700000000.5,"total":2,"with_position":1,"aircraft":[{"hex":"abc123","lat":1,"lon":2},{"hex":"def456"}]}"#);
    }

    #[test]
    fn grid_finds_what_a_scan_finds() {
        use rand::{Rng, SeedableRng};
        let mut rng = rand_chacha::ChaCha8Rng::seed_from_u64(7);
        let entries: Vec<Entry> = (0..3000)
            .map(|i| {
                let lat: f64 = rng.gen_range(-90.0..=90.0);
                let lon: f64 = rng.gen_range(-180.0..=180.0);
                let j = if i % 50 == 0 {
                    format!(r#"{{"hex":"{i:06x}"}}"#)
                } else {
                    format!(r#"{{"hex":"{i:06x}","lat":{lat},"lon":{lon}}}"#)
                };
                Entry::new(parse_aircraft(&j).unwrap())
            })
            .collect();
        let snap = Snapshot::new(1.0, entries);
        let mut idx = Vec::new();
        for _ in 0..500 {
            let (w, e): (f64, f64) = (rng.gen_range(-180.0..=180.0), rng.gen_range(-180.0..=180.0));
            let s: f64 = rng.gen_range(-90.0..=90.0);
            let n: f64 = rng.gen_range(s..=90.0);
            let b = BBox { w, s, e, n };
            snap.in_box(&b, &mut idx);
            let scan: Vec<u32> = (0..snap.entries.len() as u32).filter(|&i| snap.entries[i as usize].in_bbox(&b)).collect();
            assert_eq!(idx, scan, "{b:?}");
        }
        for b in [BBox { w: -180.0, s: -90.0, e: 180.0, n: 90.0 }, BBox { w: 180.0, s: 90.0, e: 180.0, n: 90.0 }] {
            snap.in_box(&b, &mut idx);
            let scan: Vec<u32> = (0..snap.entries.len() as u32).filter(|&i| snap.entries[i as usize].in_bbox(&b)).collect();
            assert_eq!(idx, scan, "{b:?}");
        }
    }

    #[test]
    fn bbox_crosses_antimeridian() {
        let e = Entry::new(vals(r#"{"hex":"a","lat":10,"lon":179.5}"#));
        assert!(e.in_bbox(&BBox { w: 170.0, s: 0.0, e: -170.0, n: 20.0 }));
        assert!(!e.in_bbox(&BBox { w: -170.0, s: 0.0, e: 170.0, n: 20.0 }));
    }
}
