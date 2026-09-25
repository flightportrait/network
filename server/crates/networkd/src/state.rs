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
    /// Postgres for the routes that read what changes during the day
    /// (the community catalog, the airframe record); None: they forward
    pub db: Option<crate::pg::Lazy>,
    /// Stations waiting for setup, by home network (memory only)
    pub beacons: Mutex<crate::beacon::Beacons>,
    /// the gaps artifact (routes settled at one end only)
    pub gaps: Arc<crate::gapbook::GapBook>,
    /// the observed routes artifact
    pub routes: Arc<crate::routebook::RouteBook>,
    /// the position estimator, when this instance runs it
    pub estimates: Option<Arc<crate::estimate::Estimator>>,
    /// the instance's own state file, when it has no Postgres
    pub local: Option<Arc<crate::localdb::LocalDb>>,
    /// the stations registry, when this instance keeps it
    pub stations: Option<Arc<crate::stations::Registry>>,
    /// the private fleet tier's lookups, remembered between requests
    pub fleet: crate::fleet::Caches,
    published: watch::Sender<u64>,
}

impl App {
    pub fn new(settings: Settings) -> Arc<App> {
        let (published, _) = watch::channel(0);
        let routes = crate::routebook::RouteBook::new(&settings.routes_path);
        // without Postgres, the writers keep the instance's own state file
        let writers = settings.squawks || settings.stations || settings.estimates || settings.nat;
        let local = if settings.database_url.is_empty() && !settings.point_mode() && writers {
            match crate::localdb::LocalDb::open(&settings.state_path) {
                Ok(db) => Some(Arc::new(db)),
                Err(e) => {
                    eprintln!("state: {}: {e}", settings.state_path);
                    None
                }
            }
        } else {
            None
        };
        let stored = !settings.database_url.is_empty() || local.is_some();
        let estimates = (settings.estimates && !settings.point_mode())
            .then(|| crate::estimate::Estimator::new(routes.clone(), &settings.database_url, local.clone()));
        let stations = (settings.stations && !settings.point_mode()).then_some(()).and_then(|_| {
            if !settings.database_url.is_empty() {
                Some(crate::stations::Registry::new(&settings.database_url))
            } else {
                local.clone().map(crate::stations::Registry::local)
            }
        });
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
            squawks: (settings.squawks && !settings.point_mode() && stored)
                .then(|| Arc::new(Mutex::new(crate::squawks::Watcher::default()))),
            beacons: crate::beacon::Beacons::new(),
            gaps: crate::gapbook::GapBook::new(&settings.gaps_path),
            routes,
            estimates,
            db: (!settings.database_url.is_empty()).then(|| crate::pg::Lazy::new(&settings.database_url)),
            stations,
            local,
            fleet: crate::fleet::Caches::default(),
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
