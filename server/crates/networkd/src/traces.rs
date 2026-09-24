//! Rolling position history per aircraft, recorded from the published
//! snapshots every couple of seconds. In memory only, bounded in both
//! points per aircraft and aircraft held.
//!
//! Points are packed (48 bytes, no heap per point): the time as tenths
//! of a second, and each number as the exact double readsb sent plus
//! whether it was an int, a float, "ground" or absent.

use std::collections::{HashMap, VecDeque};

use crate::pyjson::{write_float, Val};
use crate::sky::{Snapshot, F_ALT, F_LAT, F_LON, F_TRACK};

/// A number as readsb sent it: int or float, or "ground", or absent.
/// Packed as a double plus a kind, so a point stays small.
#[derive(Clone, Copy, PartialEq, Debug)]
#[repr(u8)]
enum Kind {
    None,
    Ground,
    Int,
    Float,
}

#[derive(Clone, Copy, Debug)]
struct Point {
    ds: i64, // round(t, 1) as tenths of a second
    nums: [f64; 4],
    kinds: [Kind; 4],
}

const P_LAT: usize = 0;
const P_LON: usize = 1;
const P_ALT: usize = 2;
const P_TRACK: usize = 3;

fn num(v: Option<&Val>) -> (f64, Kind) {
    match v {
        Some(Val::Int(i)) => (*i as f64, Kind::Int),
        Some(Val::UInt(u)) => (*u as f64, Kind::Int),
        Some(Val::Float(f)) => (*f, Kind::Float),
        Some(Val::Str(s)) if &**s == "ground" => (0.0, Kind::Ground),
        _ => (0.0, Kind::None),
    }
}

fn write_num(out: &mut String, x: f64, k: Kind) {
    match k {
        Kind::None => out.push_str("null"),
        Kind::Ground => out.push_str("\"ground\""),
        Kind::Int => out.push_str(&(x as i64).to_string()),
        Kind::Float => write_float(out, x),
    }
}

struct Trace {
    points: VecDeque<Point>,
    last_seen: f64,
}

pub struct TraceBook {
    retention_s: f64,
    max_points: usize,
    max_aircraft: usize,
    traces: HashMap<Box<str>, Trace>,
}

fn canonical_hex(h: &str) -> Option<String> {
    let h = h.trim().to_ascii_lowercase();
    (h.len() == 6 && h.bytes().all(|b| b.is_ascii_hexdigit())).then_some(h)
}

impl TraceBook {
    pub fn new(retention_s: f64, max_points: usize, max_aircraft: usize) -> TraceBook {
        TraceBook { retention_s, max_points, max_aircraft, traces: HashMap::new() }
    }

    pub fn record(&mut self, snap: &Snapshot) {
        let now = if snap.generated_at != 0.0 { snap.generated_at } else { crate::state::now_s() };
        let ds = (crate::pyjson::round_to(now, 1) * 10.0).round() as i64;
        for e in &snap.entries {
            // a position, as `entry.get("lat") is not None`
            if !e.vals[F_LAT].as_ref().is_some_and(|v| !v.is_null()) {
                continue;
            }
            let Some(hex) = canonical_hex(&e.hex) else { continue };
            if !self.traces.contains_key(hex.as_str()) && self.traces.len() >= self.max_aircraft {
                self.evict_oldest();
            }
            let max_points = self.max_points;
            let trace = self
                .traces
                .entry(hex.into())
                .or_insert_with(|| Trace { points: VecDeque::with_capacity(8), last_seen: now });
            let mut p = Point { ds, nums: [0.0; 4], kinds: [Kind::None; 4] };
            for (slot, f) in [(P_LAT, F_LAT), (P_LON, F_LON), (P_ALT, F_ALT), (P_TRACK, F_TRACK)] {
                (p.nums[slot], p.kinds[slot]) = num(e.vals[f].as_ref());
            }
            // a hovering point refreshes its time instead of repeating
            match trace.points.back_mut() {
                Some(last)
                    if last.nums[P_LAT] == p.nums[P_LAT]
                        && last.nums[P_LON] == p.nums[P_LON]
                        && (last.kinds[P_LON] == Kind::None) == (p.kinds[P_LON] == Kind::None) =>
                {
                    last.ds = p.ds
                }
                _ => {
                    if trace.points.len() >= max_points {
                        trace.points.pop_front();
                    }
                    trace.points.push_back(p);
                }
            }
            trace.last_seen = now;
        }
        self.prune(now);
    }

    fn evict_oldest(&mut self) {
        if let Some(oldest) = self
            .traces
            .iter()
            .min_by(|a, b| a.1.last_seen.total_cmp(&b.1.last_seen))
            .map(|(k, _)| k.clone())
        {
            self.traces.remove(&oldest);
        }
    }

    fn prune(&mut self, now: f64) {
        let r = self.retention_s;
        self.traces.retain(|_, t| now - t.last_seen <= r);
    }

    /// `[[t, lat, lon, alt_baro, track], ...]`, oldest first.
    pub fn get_json(&self, hex: &str) -> Option<String> {
        let t = self.traces.get(hex.trim().to_lowercase().as_str())?;
        if t.points.is_empty() {
            return None;
        }
        let mut out = String::with_capacity(t.points.len() * 48);
        out.push('[');
        for (i, p) in t.points.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            out.push('[');
            write_float(&mut out, p.ds as f64 / 10.0);
            for k in 0..4 {
                out.push(',');
                write_num(&mut out, p.nums[k], p.kinds[k]);
            }
            out.push(']');
        }
        out.push(']');
        Some(out)
    }

    pub fn len(&self) -> usize {
        self.traces.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sky::{parse_aircraft, Entry};

    fn snap(t: f64, rows: &[&str]) -> Snapshot {
        Snapshot::new(t, rows.iter().map(|r| Entry::new(parse_aircraft(r).unwrap())).collect())
    }

    #[test]
    fn records_dedupes_and_caps() {
        let mut b = TraceBook::new(1800.0, 2, 10);
        b.record(&snap(100.04, &[r#"{"hex":"ABCDEF","lat":1.5,"lon":2.25,"alt_baro":"ground","track":90}"#, r#"{"hex":"zz","lat":1,"lon":1}"#]));
        b.record(&snap(102.0, &[r#"{"hex":"abcdef","lat":1.5,"lon":2.25,"alt_baro":1000}"#]));
        assert_eq!(b.get_json("abcdef").unwrap(), r#"[[102.0,1.5,2.25,"ground",90]]"#);
        b.record(&snap(104.0, &[r#"{"hex":"abcdef","lat":1.6,"lon":2.25,"alt_baro":1000}"#]));
        b.record(&snap(106.0, &[r#"{"hex":"abcdef","lat":1.7,"lon":2.25,"alt_baro":1025.5,"track":null}"#]));
        assert_eq!(b.get_json("ABCDEF ").unwrap(), r#"[[104.0,1.6,2.25,1000,null],[106.0,1.7,2.25,1025.5,null]]"#);
        assert_eq!(b.len(), 1, "non-canonical hex ids are not admitted");
        b.record(&snap(106.0 + 1801.0, &[]));
        assert!(b.get_json("abcdef").is_none());
    }
}
