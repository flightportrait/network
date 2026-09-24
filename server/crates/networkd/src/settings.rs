//! Runtime configuration: the same NETWORK_API_* environment as the
//! Python service, same defaults, so one .env drives either.

use std::env;

fn s(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_string())
}

fn int(name: &str, default: i64) -> i64 {
    env::var(name).ok().filter(|v| !v.is_empty()).and_then(|v| v.trim().parse().ok()).unwrap_or(default)
}

fn float(name: &str, default: f64) -> f64 {
    env::var(name).ok().filter(|v| !v.is_empty()).and_then(|v| v.trim().parse().ok()).unwrap_or(default)
}

#[derive(Clone, Debug)]
pub struct Settings {
    pub bind: String,
    pub live_json: String,
    pub upstream_url: String,
    pub upstream_timeout_s: f64,
    pub source_mode: String,
    pub source_point_url: String,
    pub source_lat: f64,
    pub source_lon: f64,
    pub source_radius_nm: i64,
    pub snapshot_poll_s: f64,
    pub station_poll_s: f64,
    pub stale_after_s: i64,
    pub station_presence_stale_s: i64,
    pub max_point_radius_nm: i64,
    pub trace_retention_s: i64,
    pub trace_max_points: usize,
    pub trace_max_aircraft: usize,
    pub trace_rate_limit: usize,
    pub legs_path: String,
    pub max_aircraft: usize,
    pub now_rate_limit: usize,
    pub aircraft_rate_limit: usize,
    pub stream_max_per_ip: usize,
    pub point_rate_limit: usize,
    pub rate_window_s: u64,
    pub client_ip_header: String,
    pub cors_origins: Vec<String>,
    pub source_url: String,
    pub terms_url: String,
    pub attribution: String,
    pub credits_url: String,
    /// permessage-deflate with a compressor kept per socket (smaller
    /// frames, ~256 KB of memory per socket) instead of per message
    pub deflate_takeover: bool,
    /// where routes networkd does not serve are forwarded (empty: 404)
    pub fallback: String,
    /// the nightly reference snapshot (api: python -m app.refdata_export)
    pub refdata_path: String,
    pub refdata_rate_limit: usize,
}

impl Settings {
    pub fn from_env() -> Settings {
        Settings {
            bind: s("NETWORKD_BIND", "0.0.0.0:8092"),
            live_json: s("NETWORK_API_LIVE_JSON", ""),
            upstream_url: s("NETWORK_API_UPSTREAM", "http://aggregator:80"),
            upstream_timeout_s: float("NETWORK_API_UPSTREAM_TIMEOUT_S", 2.0),
            source_mode: s("NETWORK_API_SOURCE_MODE", "readsb"),
            source_point_url: s(
                "NETWORK_API_SOURCE_POINT_URL",
                "https://data.flightportrait.com/v2/point/{lat}/{lon}/{radius}",
            ),
            source_lat: float("NETWORK_API_SOURCE_LAT", 1.3521),
            source_lon: float("NETWORK_API_SOURCE_LON", 103.8198),
            source_radius_nm: int("NETWORK_API_SOURCE_RADIUS_NM", 250),
            snapshot_poll_s: float("NETWORK_API_SNAPSHOT_POLL_S", 2.0),
            station_poll_s: float("NETWORK_API_STATION_POLL_S", 15.0),
            stale_after_s: int("NETWORK_API_STALE_AFTER_S", 60),
            station_presence_stale_s: int("NETWORK_API_STATION_PRESENCE_STALE_S", 90),
            max_point_radius_nm: int("NETWORK_API_MAX_RADIUS_NM", 250),
            trace_retention_s: int("NETWORK_API_TRACE_RETENTION_S", 1800),
            trace_max_points: int("NETWORK_API_TRACE_MAX_POINTS", 720) as usize,
            trace_max_aircraft: int("NETWORK_API_TRACE_MAX_AIRCRAFT", 20000) as usize,
            trace_rate_limit: int("NETWORK_API_TRACE_RATE_LIMIT", 600) as usize,
            legs_path: s("NETWORK_API_LEGS_PATH", "data/legs.db"),
            max_aircraft: int("NETWORK_API_MAX_AIRCRAFT", 10000) as usize,
            now_rate_limit: int("NETWORK_API_NOW_RATE_LIMIT", 600) as usize,
            aircraft_rate_limit: int("NETWORK_API_AIRCRAFT_RATE_LIMIT", 300) as usize,
            stream_max_per_ip: int("NETWORK_API_STREAM_MAX_PER_IP", 4) as usize,
            point_rate_limit: int("NETWORK_API_POINT_RATE_LIMIT", 300) as usize,
            rate_window_s: int("NETWORK_API_RATE_WINDOW_S", 600) as u64,
            client_ip_header: s("NETWORK_API_CLIENT_IP_HEADER", "").to_lowercase(),
            cors_origins: s("NETWORK_API_ORIGINS", "*")
                .split(',')
                .map(|o| o.trim().to_string())
                .filter(|o| !o.is_empty())
                .collect(),
            source_url: s("NETWORK_API_SOURCE_URL", "https://github.com/flightportrait/network"),
            terms_url: s("NETWORK_API_TERMS_URL", "https://flightportrait.com/network/terms"),
            attribution: s(
                "NETWORK_API_ATTRIBUTION",
                "Data (c) FlightPortrait network feeders and credited sources, ODbL 1.0",
            ),
            credits_url: s("NETWORK_API_CREDITS_URL", "https://flightportrait.com/network/credits.html"),
            fallback: s("NETWORKD_FALLBACK", ""),
            refdata_path: s("NETWORKD_REFDATA_PATH", "data/refdata.sqlite"),
            refdata_rate_limit: int("NETWORK_API_REFDATA_RATE_LIMIT", 300) as usize,
            deflate_takeover: matches!(s("NETWORKD_DEFLATE_TAKEOVER", "").as_str(), "1" | "true" | "yes"),
        }
    }

    pub fn point_mode(&self) -> bool {
        self.source_mode == "point"
    }
}
