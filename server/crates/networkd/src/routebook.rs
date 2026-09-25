//! The observed routes artifact (the Python service's routes_db.py): a
//! gzipped JSON object, callsign -> [origin, ..., dest], written nightly.
//! Missing file: every lookup is None and the callers fall back.
//!
//! Loaded off the request path: a task checks the file's mtime every
//! RELOAD_S and swaps in a new table when it changed; a bad file changes
//! nothing.

use std::collections::HashMap;
use std::io::Read;
use std::sync::{Arc, RwLock};
use std::time::{Duration, SystemTime};

use serde_json::Value;

const RELOAD_S: u64 = 300;

#[derive(Default)]
struct Loaded {
    mtime: Option<SystemTime>,
    routes: Arc<HashMap<String, Value>>,
}

pub struct RouteBook {
    path: String,
    loaded: RwLock<Loaded>,
}

impl RouteBook {
    pub fn new(path: &str) -> Arc<RouteBook> {
        Arc::new(RouteBook { path: path.to_string(), loaded: RwLock::new(Loaded::default()) })
    }

    fn read(path: &str) -> anyhow::Result<HashMap<String, Value>> {
        let mut text = String::new();
        flate2::read::GzDecoder::new(std::fs::File::open(path)?).read_to_string(&mut text)?;
        let Value::Object(data) = serde_json::from_str(&text)? else {
            anyhow::bail!("not an object");
        };
        Ok(data.into_iter().map(|(k, v)| (k.trim().to_uppercase(), v)).collect())
    }

    /// Reload when the file changed; drop the table when it is gone.
    pub fn refresh(&self) {
        let mtime = std::fs::metadata(&self.path).and_then(|m| m.modified()).ok();
        let Some(mtime) = mtime else {
            *self.loaded.write().unwrap() = Loaded::default();
            return;
        };
        if self.loaded.read().unwrap().mtime == Some(mtime) {
            return;
        }
        match RouteBook::read(&self.path) {
            Ok(routes) => {
                eprintln!("routes: {} callsigns from {}", routes.len(), self.path);
                *self.loaded.write().unwrap() = Loaded { mtime: Some(mtime), routes: Arc::new(routes) };
            }
            Err(e) => eprintln!("routes: {} unreadable, keeping the last: {e}", self.path),
        }
    }

    pub async fn keep(self: Arc<RouteBook>) {
        loop {
            let b = self.clone();
            let _ = tokio::task::spawn_blocking(move || b.refresh()).await;
            tokio::time::sleep(Duration::from_secs(RELOAD_S)).await;
        }
    }

    pub fn available(&self) -> bool {
        self.loaded.read().unwrap().mtime.is_some()
    }

    pub fn get(&self, callsign: &str) -> Option<Value> {
        self.loaded.read().unwrap().routes.get(&callsign.trim().to_uppercase()).cloned()
    }
}
