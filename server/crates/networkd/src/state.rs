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
    /// connected stations by half id, when this instance keeps the registry
    pub live: HashMap<String, crate::stations::Live>,
}

pub struct App {
    pub settings: Settings,
    snapshot: SnapshotCell,
    pub live: Mutex<LiveSky>,
    pub traces: Mutex<TraceBook>,
    pub presence: Mutex<Presence>,
    pub legs: Arc<LegBook>,
    pub limiter: RateLimiter,
    pub upstream: Upstream,
    pub open_sockets: Mutex<HashMap<String, usize>>,
    pub bounds_cache: Mutex<HashMap<String, (f64, crate::departure::Bounds)>>,
    pub fallback: Option<crate::proxy::Fallback>,
    pub refdb: Arc<crate::refdb::RefDb>,
    /// the emergency-squawk watcher, when this instance records squawks
    pub squawks: Option<Arc<Mutex<crate::squawks::Watcher>>>,
    /// the observed routes artifact
    pub routes: Arc<crate::routebook::RouteBook>,
    /// the position estimator, when this instance runs it
    pub estimates: Option<Arc<crate::estimate::Estimator>>,
    /// the stations registry, when this instance keeps it
    pub stations: Option<Arc<crate::stations::Registry>>,
    published: watch::Sender<u64>,
}

impl App {
    pub fn new(settings: Settings) -> Arc<App> {
        let (published, _) = watch::channel(0);
        let routes = crate::routebook::RouteBook::new(&settings.routes_path);
        let estimates = (settings.estimates && !settings.point_mode())
            .then(|| crate::estimate::Estimator::new(routes.clone(), &settings.database_url));
        Arc::new(App {
            live: Mutex::new(LiveSky::new(settings.max_aircraft)),
            traces: Mutex::new(TraceBook::new(
                settings.trace_retention_s as f64,
                settings.trace_max_points,
                settings.trace_max_aircraft,
            )),
            presence: Mutex::new(Presence { available: true, ..Default::default() }),
            legs: LegBook::new(&settings.legs_path),
            limiter: RateLimiter::new(),
            upstream: Upstream::new(&settings.upstream_url, settings.upstream_timeout_s),
            open_sockets: Mutex::new(HashMap::new()),
            bounds_cache: Mutex::new(HashMap::new()),
            fallback: crate::proxy::Fallback::new(&settings.fallback),
            refdb: crate::refdb::RefDb::new(&settings.refdata_path),
            squawks: (settings.squawks && !settings.point_mode() && !settings.database_url.is_empty())
                .then(|| Arc::new(Mutex::new(crate::squawks::Watcher::default()))),
            routes,
            estimates,
            stations: (settings.stations && !settings.point_mode() && !settings.database_url.is_empty())
                .then(|| crate::stations::Registry::new(&settings.database_url)),
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
