"""Runtime configuration for the network API.

Plain dataclass + environment defaults so a bare checkout runs;
nothing here is secret. The service holds no credentials of its own —
whatever proxy or tunnel fronts it in a deployment owns those.
"""
import os
from dataclasses import dataclass, field


def _env(name, default):
    return os.environ.get(name, default)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # Postgres for the stations registry. Set NETWORK_API_DATABASE_URL in
    # any real deployment; the local-dev default below is a placeholder,
    # not a credential (loopback only, never used with a real password).
    database_url: str = field(default_factory=lambda: _env(
        "NETWORK_API_DATABASE_URL",
        "postgresql+psycopg://localhost/netapi"))

    # The local readsb this API reads, over the deployment's private
    # network. Never a public URL — this service sits NEXT to the
    # aggregator by design.
    upstream_url: str = field(default_factory=lambda: _env(
        "NETWORK_API_UPSTREAM", "http://aggregator:80"))
    upstream_timeout_s: float = field(default_factory=lambda: _env_float(
        "NETWORK_API_UPSTREAM_TIMEOUT_S", 2.0))

    # Without a local receiver the live sky can come from a public v2
    # point endpoint instead of a local readsb. Whoever runs it picks the
    # source and owns its terms (adsb.lol is ODbL; some others are
    # personal-use). Remote mode enforces a polite poll floor.
    source_mode: str = field(default_factory=lambda: _env(
        "NETWORK_API_SOURCE_MODE", "readsb"))       # readsb | point
    source_point_url: str = field(default_factory=lambda: _env(
        "NETWORK_API_SOURCE_POINT_URL",
        "https://data.flightportrait.com/v2/point/{lat}/{lon}/{radius}"))
    source_lat: float = field(default_factory=lambda: _env_float(
        "NETWORK_API_SOURCE_LAT", 1.3521))
    source_lon: float = field(default_factory=lambda: _env_float(
        "NETWORK_API_SOURCE_LON", 103.8198))
    source_radius_nm: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_SOURCE_RADIUS_NM", 250))

    # Pollers. The snapshot poll is the ONLY steady load this API puts on
    # readsb, independent of public traffic — that is the point.
    snapshot_poll_s: float = field(default_factory=lambda: _env_float(
        "NETWORK_API_SNAPSHOT_POLL_S", 5.0))
    station_poll_s: float = field(default_factory=lambda: _env_float(
        "NETWORK_API_STATION_POLL_S", 15.0))
    receivers_poll_s: float = field(default_factory=lambda: _env_float(
        "NETWORK_API_RECEIVERS_POLL_S", 300.0))

    # A snapshot older than this is a dead sky and is served as 503, never
    # as live data. Must comfortably exceed snapshot_poll_s.
    stale_after_s: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_STALE_AFTER_S", 60))
    # A station unseen for this long counts as offline in rosters/counters.
    offline_after_s: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_OFFLINE_AFTER_S", 60))

    max_point_radius_nm: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_MAX_RADIUS_NM", 250))
    # Trails: how much position history the API remembers per aircraft.
    # In-memory by design; a restart forgets and the trails regrow.
    trace_retention_s: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_TRACE_RETENTION_S", 1800))
    trace_max_points: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_TRACE_MAX_POINTS", 720))
    trace_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_TRACE_RATE_LIMIT", 600))
    # The ODbL routes artifact (callsign -> [origin, dest]), delivered
    # as a file by the operator's export pipeline. Absent = 404s.
    routes_path: str = field(default_factory=lambda: _env(
        "NETWORK_API_ROUTES_PATH", "data/routes.json.gz"))
    route_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_ROUTE_RATE_LIMIT", 600))
    # Per-airframe flight logs (SQLite artifact, legs_db.py).
    legs_path: str = field(default_factory=lambda: _env(
        "NETWORK_API_LEGS_PATH", "data/legs.db"))
    airframe_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_AIRFRAME_RATE_LIMIT", 300))
    airport_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_AIRPORT_RATE_LIMIT", 120))
    # Reference-data lookups: open, cached hard, one
    # shared bucket — slow-moving data never earns a bigger slice of the
    # box than the live sky.
    refdata_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_REFDATA_RATE_LIMIT", 300))
    # A flight number earns a timetable row only when observed this many
    # times within the schedule derive's recent window: filters one-off
    # charters while a weekly service still qualifies.
    schedule_min_flights: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_SCHEDULE_MIN_FLIGHTS", 5))
    max_aircraft: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_MAX_AIRCRAFT", 10000))
    session_retention_days: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_SESSION_RETENTION_DAYS", 90))

    # Privacy floor for the public roster: 0.1 degree (~11 km) rounding.
    # Deliberately NOT env-tunable — the promise to feeders does not vary
    # by deployment.
    coarse_decimals: int = 1

    # Rate limits, per bucket per rate_window_s. Open data, but a guest's
    # share of a small VPS.
    now_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_NOW_RATE_LIMIT", 600))
    aircraft_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_AIRCRAFT_RATE_LIMIT", 300))
    point_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_POINT_RATE_LIMIT", 300))
    stations_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_STATIONS_RATE_LIMIT", 120))
    station_detail_rate_limit: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_STATION_DETAIL_RATE_LIMIT", 60))
    rate_window_s: int = field(default_factory=lambda: _env_int(
        "NETWORK_API_RATE_WINDOW_S", 600))

    # Trusted client-IP header. Behind a trusted reverse proxy this is
    # the proxy's real-client header (e.g. cf-connecting-ip); blank means
    # "trust the socket peer" and MUST stay blank on any directly-exposed
    # deployment, or clients could choose their own identity.
    client_ip_header: str = field(default_factory=lambda: _env(
        "NETWORK_API_CLIENT_IP_HEADER", "").lower())

    # CORS. "*" is deliberate: this is a credential-less open-data API, so
    # the allowlist doctrine used where cookies could exist does not apply.
    cors_origins: str = field(default_factory=lambda: _env(
        "NETWORK_API_ORIGINS", "*"))

    # Strings surfaced on "/" so consumers can find the rules of the road.
    source_url: str = field(default_factory=lambda: _env(
        "NETWORK_API_SOURCE_URL",
        "https://github.com/flightportrait/network"))
    terms_url: str = field(default_factory=lambda: _env(
        "NETWORK_API_TERMS_URL",
        "https://flightportrait.com/network/terms"))
    attribution: str = field(default_factory=lambda: _env(
        "NETWORK_API_ATTRIBUTION",
        "Data (c) FlightPortrait network feeders, ODbL 1.0"))

    @property
    def cors_origin_list(self):
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
