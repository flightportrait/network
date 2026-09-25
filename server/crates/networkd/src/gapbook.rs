//! The gaps artifact (the Python service's gaps_db.py): callsigns whose
//! route observation settled at one end only, published as questions.
//! gaps.json.gz: {callsign: {side, known, hint, n_recent, last_seen,
//! last_lat, last_lon, last_trk}}. Reloaded off the request path when
//! the file changes, like the routes artifact.

use std::collections::HashMap;
use std::io::Read;
use std::sync::{Arc, RwLock};
use std::time::{Duration, SystemTime};

use serde_json::{Map, Value};

const RELOAD_S: u64 = 300;

/// A route belongs to a flight number, `^[A-Z]{3}\d{1,4}[A-Z]{0,2}$`: a
/// registration flying as its own callsign has nowhere to be asked about.
pub fn flight_number(s: &str) -> bool {
    let b = s.as_bytes();
    if b.len() < 4 || !b[..3].iter().all(u8::is_ascii_uppercase) {
        return false;
    }
    let digits = b[3..].iter().take_while(|c| c.is_ascii_digit()).count();
    let rest = &b[3 + digits..];
    (1..=4).contains(&digits) && rest.len() <= 2 && rest.iter().all(u8::is_ascii_uppercase)
}

#[derive(Default)]
pub struct Gaps {
    pub by_callsign: HashMap<String, Map<String, Value>>,
    /// most asked first: n_recent descending, then callsign
    pub ordered: Vec<String>,
}

#[derive(Default)]
struct Loaded {
    mtime: Option<SystemTime>,
    gaps: Arc<Gaps>,
}

pub struct GapBook {
    path: String,
    loaded: RwLock<Loaded>,
}

/// Python's `int(x or 0)` for n_recent.
fn n_recent(v: &Map<String, Value>) -> i64 {
    match v.get("n_recent") {
        Some(Value::Number(n)) => n.as_i64().or_else(|| n.as_f64().map(|f| f.trunc() as i64)).unwrap_or(0),
        Some(Value::String(s)) => s.trim().parse().unwrap_or(0),
        Some(Value::Bool(b)) => *b as i64,
        _ => 0,
    }
}

impl GapBook {
    pub fn new(path: &str) -> Arc<GapBook> {
        Arc::new(GapBook { path: path.to_string(), loaded: RwLock::new(Loaded::default()) })
    }

    fn read(path: &str) -> anyhow::Result<Gaps> {
        let mut text = String::new();
        flate2::read::GzDecoder::new(std::fs::File::open(path)?).read_to_string(&mut text)?;
        let Value::Object(data) = serde_json::from_str(&text)? else {
            anyhow::bail!("not an object");
        };
        let mut by_callsign = HashMap::new();
        for (k, v) in data {
            let k = k.trim().to_uppercase();
            if let Value::Object(v) = v {
                if flight_number(&k) {
                    by_callsign.insert(k, v);
                }
            }
        }
        let mut ordered: Vec<(i64, String)> = by_callsign.iter().map(|(k, v)| (-n_recent(v), k.clone())).collect();
        ordered.sort();
        Ok(Gaps { by_callsign, ordered: ordered.into_iter().map(|(_, k)| k).collect() })
    }

    pub fn refresh(&self) {
        let Some(mtime) = std::fs::metadata(&self.path).and_then(|m| m.modified()).ok() else {
            *self.loaded.write().unwrap() = Loaded::default();
            return;
        };
        if self.loaded.read().unwrap().mtime == Some(mtime) {
            return;
        }
        match GapBook::read(&self.path) {
            Ok(gaps) => {
                eprintln!("gaps: {} questions from {}", gaps.by_callsign.len(), self.path);
                *self.loaded.write().unwrap() = Loaded { mtime: Some(mtime), gaps: Arc::new(gaps) };
            }
            Err(e) => eprintln!("gaps: {} unreadable, keeping the last: {e}", self.path),
        }
    }

    pub async fn keep(self: Arc<GapBook>) {
        loop {
            let b = self.clone();
            let _ = tokio::task::spawn_blocking(move || b.refresh()).await;
            tokio::time::sleep(Duration::from_secs(RELOAD_S)).await;
        }
    }

    pub fn available(&self) -> bool {
        self.loaded.read().unwrap().mtime.is_some()
    }

    pub fn all(&self) -> Arc<Gaps> {
        self.loaded.read().unwrap().gaps.clone()
    }

    pub fn get(&self, callsign: &str) -> Option<Map<String, Value>> {
        self.all().by_callsign.get(&callsign.trim().to_uppercase()).cloned()
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn flight_numbers() {
        for ok in ["SIA1", "SIA1234", "BAW12AB", "EZY95TZ"] {
            assert!(super::flight_number(ok), "{ok}");
        }
        for bad in ["SI1", "SIA", "SIA12345", "SIA1ABC", "9VSWA", "sia1", "SIAA1"] {
            assert!(!super::flight_number(bad), "{bad}");
        }
    }
}
