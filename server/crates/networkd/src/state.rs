//! Everything the routes and tasks share.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use tokio::sync::watch;

use crate::legs::LegBook;
use crate::ratelimit::RateLimiter;
use crate::settings::Settings;
use crate::sky::{LiveSky, Snapshot, SnapshotCell};
use crate::traces::TraceBook;
use crate::upstream::Upstream;

pub fn now_s() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_secs_f64()
}

#[derive(Default)]
pub struct Presence {
    pub count: usize,
    pub available: bool,
    pub at: f64,
}

pub struct App {
    pub settings: Settings,
    snapshot: SnapshotCell,
    pub live: Mutex<LiveSky>,
    pub traces: Mutex<TraceBook>,
    pub presence: Mutex<Presence>,
    pub legs: LegBook,
    pub limiter: RateLimiter,
    pub upstream: Upstream,
    pub open_sockets: Mutex<HashMap<String, usize>>,
    pub bounds_cache: Mutex<HashMap<String, (f64, crate::departure::Bounds)>>,
    published: watch::Sender<u64>,
}

impl App {
    pub fn new(settings: Settings) -> Arc<App> {
        let (published, _) = watch::channel(0);
        Arc::new(App {
            live: Mutex::new(LiveSky::new(settings.max_aircraft)),
            traces: Mutex::new(TraceBook::new(
                settings.trace_retention_s as f64,
                settings.trace_max_points,
                settings.trace_max_aircraft,
            )),
            presence: Mutex::new(Presence { count: 0, available: true, at: 0.0 }),
            legs: LegBook::new(&settings.legs_path),
            limiter: RateLimiter::new(),
            upstream: Upstream::new(&settings.upstream_url, settings.upstream_timeout_s),
            open_sockets: Mutex::new(HashMap::new()),
            bounds_cache: Mutex::new(HashMap::new()),
            snapshot: SnapshotCell::new(),
            published,
            settings,
        })
    }

    pub fn snapshot(&self) -> Arc<Snapshot> {
        self.snapshot.get()
    }

    pub fn set_snapshot(&self, s: Arc<Snapshot>) {
        self.snapshot.set(s);
    }

    /// Wake every socket waiting for a new snapshot.
    pub fn bump(&self) {
        self.published.send_modify(|n| *n += 1);
    }

    pub fn subscribe(&self) -> watch::Receiver<u64> {
        self.published.subscribe()
    }
}
