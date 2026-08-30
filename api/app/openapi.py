"""OpenAPI metadata and response schemas. Documentation only; nothing here
changes responses. The schemas are the public contract: a codegen client
built from them must not lie, so nullability, units, and the two field
dialects (readsb passthrough on the live list, full words everywhere else)
are declared explicitly.
"""
from typing import Annotated

from fastapi import Path

SERVERS = [{"url": "https://data.flightportrait.com"}]

TAGS = [
    {"name": "Live", "description": "What the network hears now."},
    {"name": "History", "description": "Observed airframes, flights, "
                                       "airports."},
    {"name": "Stations", "description": "Feeder roster."},
    {"name": "Reference", "description": "Airlines, alliances, types."},
    {"name": "Meta", "description": "Index and health."},
]

DESCRIPTION = (
    "Open data from the FlightPortrait receiver network. No API key.\n"
    "\n"
    "**Stability.** Operations marked `x-stability: stable` only ever gain "
    "fields; names and types are frozen. Operations marked `x-stability: "
    "map` exist for the first-party map and can change with it.\n"
    "\n"
    "**Field dialects.** `/v1/aircraft` and `/v2/point` pass readsb's wire "
    "fields through unchanged (`hex`, `t`, `r`, `gs`, ...) so ecosystem "
    "tooling works as-is. Every other resource uses full words: `reg`, "
    "`type`, `org`, `dst`. Airport codes in history resources are IATA; "
    "`/v1/airports/{code}` accepts IATA or ICAO. Event times are unix "
    "seconds UTC; registry timestamps are ISO 8601 UTC; board and schedule "
    "times are HH:MM in the origin airport's local time.\n"
    "\n"
    "**Data honesty.** Everything here is observation, never a published "
    "schedule or an official registry. Gaps mean the network's sources did "
    "not hear it, nothing more. History responses carry `coverage: "
    "\"observed\"` as a reminder.\n"
    "\n"
    "**Errors.** Every non-200 body is `{\"error\": <code>, \"detail\": "
    "<human text>}` with `Cache-Control: no-store`. 404 `not_found` / "
    "`not_observed`, 422 `invalid_request`, 429 `rate_limited` (with "
    "`Retry-After` and `RateLimit-*` headers), 503 `stale_snapshot` (live "
    "snapshot older than 60 s) or `artifact_unavailable` (a history "
    "artifact is not loaded).\n"
    "\n"
    "**Rate limits.** Per IP, per bucket, over a 600 second window; each "
    "operation notes its bucket and default limit. 429 means wait for "
    "`Retry-After` seconds.\n"
    "\n"
    "Data is ODbL 1.0. Credit \"FlightPortrait network feeders\".\n"
    "Terms: https://flightportrait.com/network/terms"
)

Hex = Annotated[str, Path(
    description="ICAO 24-bit address, 6 hex chars. Case-insensitive.")]
Callsign = Annotated[str, Path(
    description="Callsign or flight ident, 2-12 alphanumerics.")]
AirportCode = Annotated[str, Path(
    description="IATA or ICAO airport code.")]
AirlineICAO = Annotated[str, Path(
    description="Airline ICAO code.")]
AllianceSlug = Annotated[str, Path(
    description="Alliance slug, for example star-alliance.")]
AirportEnd = Annotated[str, Path(
    description="Airport code (IATA). The pair is undirected; order does "
                "not matter.")]
TypeCode = Annotated[str, Path(
    description="ICAO type designator, for example A359.")]
StationUUID = Annotated[str, Path(
    description="Full feeder UUID. The server stores a hash, not the UUID.")]
Lat = Annotated[float, Path(description="Latitude, degrees.")]
Lon = Annotated[float, Path(description="Longitude, degrees.")]
RadiusNM = Annotated[float, Path(
    description="Radius in nautical miles. Capped at 250.")]

STABLE = {"x-stability": "stable"}
MAP_TIER = {"x-stability": "map"}
HIDDEN = {"x-hidden": True, "x-stability": "stable"}


# ---- schema helpers -------------------------------------------------

def _obj(props, required=None, description=None):
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    if description:
        schema["description"] = description
    return schema


def _arr(items, description=None):
    schema = {"type": "array", "items": items}
    if description:
        schema["description"] = description
    return schema


def _t(type_, description=None, nullable=False, **kw):
    schema = {"type": [type_, "null"] if nullable else type_}
    if description:
        schema["description"] = description
    schema.update(kw)
    return schema


ERROR_SCHEMA = _obj({
    "error": _t("string", "Machine code: not_found, not_observed, "
                          "invalid_request, rate_limited, stale_snapshot, "
                          "artifact_unavailable, upstream_unavailable."),
    "detail": _t("string", "Human-readable explanation."),
}, required=["error", "detail"])

_ERR_CONTENT = {"application/json": {"schema": ERROR_SCHEMA}}

R429 = {429: {
    "description": "Rate limited. Wait Retry-After seconds. Headers: "
                   "Retry-After, RateLimit-Limit, RateLimit-Remaining, "
                   "RateLimit-Reset.",
    "content": {"application/json": {
        "schema": ERROR_SCHEMA,
        "example": {"error": "rate_limited", "detail": "slow down"}}},
}}
R404 = {404: {"description": "Not found or not observed.",
              "content": _ERR_CONTENT}}
R422 = {422: {"description": "Malformed input.", "content": _ERR_CONTENT}}
R503 = {503: {
    "description": "Live snapshot older than 60 seconds (stale_snapshot) "
                   "or a required history artifact is not loaded "
                   "(artifact_unavailable). Retry-After is set.",
    "content": _ERR_CONTENT,
}}


def ok(example, *extra, schema=None):
    content = {"application/json": {"example": example}}
    if schema is not None:
        content["application/json"]["schema"] = schema
    responses = {200: {"description": "OK", "content": content}}
    for d in extra:
        responses.update(d)
    return responses


# ---- live -----------------------------------------------------------

SCH_NOW = _obj({
    "aircraft_count": _t("integer", "Aircraft in the current snapshot."),
    "aircraft_with_pos": _t("integer", "Of those, with a position."),
    "station_count": _t("integer", "Feeders connected right now. Null when "
                                   "presence is unavailable, never zero "
                                   "for that case.", nullable=True),
    "generated_at": _t("number", "Snapshot time, unix seconds UTC. Never "
                                 "older than 60 s (503 instead)."),
}, required=["aircraft_count", "aircraft_with_pos", "station_count",
             "generated_at"])

EX_NOW = {
    "aircraft_count": 7,
    "aircraft_with_pos": 5,
    "station_count": 1,
    "generated_at": 1787924061.0,
}

SCH_AIRCRAFT_ITEM = _obj({
    "hex": _t("string", "ICAO 24-bit address, lowercase hex."),
    "flight": _t("string", "Callsign, trailing spaces stripped. Absent "
                           "when not broadcast."),
    "t": _t("string", "ICAO type designator."),
    "r": _t("string", "Registration."),
    "lat": _t("number", "WGS84 degrees."),
    "lon": _t("number", "WGS84 degrees."),
    "alt_baro": {"description": "Barometric altitude, feet, or the string "
                                "\"ground\".",
                 "oneOf": [{"type": "integer"}, {"const": "ground"}]},
    "gs": _t("number", "Ground speed, knots."),
    "track": _t("number", "Track, degrees true."),
    "category": _t("string", "ADS-B emitter category (A0-D7)."),
    "squawk": _t("string", "Transponder code, 4 octal digits."),
    "seen": _t("number", "Seconds since the last message."),
    "seen_pos": _t("number", "Seconds since the last position."),
}, required=["hex"],
    description="readsb passthrough dialect. Optional fields are absent "
                "when unknown, never null.")

SCH_AIRCRAFT = _obj({
    "generated_at": _t("number", "Snapshot time, unix seconds UTC."),
    "aircraft": _arr(SCH_AIRCRAFT_ITEM),
}, required=["generated_at", "aircraft"])

EX_AIRCRAFT = {
    "generated_at": 1787924061.0,
    "aircraft": [{
        "hex": "76cd06", "flight": "SIA123", "t": "A359", "r": "9V-SHF",
        "lat": 1.5, "lon": 103.8, "alt_baro": 6325, "gs": 285.9,
        "track": 85.4, "category": "A5", "squawk": "2136",
        "seen": 0.2, "seen_pos": 1.1,
    }],
}

SCH_TRACE = _obj({
    "hex": _t("string"),
    "points": _arr(
        {"type": "array",
         "prefixItems": [
             _t("number", "Unix seconds UTC."),
             _t("number", "Latitude, degrees."),
             _t("number", "Longitude, degrees."),
             {"description": "Barometric altitude, feet, \"ground\", or "
                             "null.",
              "oneOf": [{"type": "integer"}, {"const": "ground"},
                        {"type": "null"}]},
             _t("number", "Track, degrees true.", nullable=True),
         ]},
        description="[t, lat, lon, alt_baro, track], oldest first."),
}, required=["hex", "points"])

EX_TRACE = {
    "hex": "76cd06",
    "points": [
        [1787924000.0, 1.48, 103.7, 6000, 84.0],
        [1787924061.0, 1.50, 103.8, 6325, 85.4],
    ],
}

# The point item is the live aircraft item plus a distance field.
SCH_POINT_ITEM = _obj(
    dict(SCH_AIRCRAFT_ITEM["properties"],
         dst=_t("number", "Distance from the query point, nautical miles.")),
    required=["hex", "dst"],
    description="readsb passthrough fields plus dst (distance, nm).")

SCH_POINT = _obj({
    "ac": _arr(SCH_POINT_ITEM,
               description="Snapshot aircraft within the radius, nearest "
                           "first."),
    "msg": _t("string", "Always \"No error\" on 200."),
    "now": _t("number", "Snapshot time, unix seconds UTC."),
    "total": _t("integer"),
    "ctime": _t("number", "Same as now; envelope compatibility."),
    "ptime": _t("number", "Processing time, ms."),
}, required=["ac", "msg", "now", "total", "ctime", "ptime"])

EX_POINT = {
    "ac": [{"hex": "76cd06", "flight": "SIA123", "t": "A359",
            "lat": 1.5, "lon": 103.8, "alt_baro": 6325, "gs": 285.9,
            "track": 85.4, "seen": 0.2, "seen_pos": 1.1, "dst": 9.4}],
    "msg": "No error",
    "now": 1787924061.0,
    "total": 1,
    "ctime": 1787924061.0,
    "ptime": 0.4,
}

# ---- history --------------------------------------------------------

_SCH_COVERAGE = _t(
    "string", "Always \"observed\": evidence from our receivers and open "
              "trace archives, never a published schedule or registry.",
    const="observed")

_SCH_WINDOW = _t("integer", "Days of history the log covers. Null when the "
                            "log artifact is not loaded.", nullable=True)

SCH_AIRFRAME_LEG = _obj({
    "date": _t("string", "YYYY-MM-DD, UTC."),
    "org": _t("string", "Origin, IATA.", nullable=True),
    "dst": _t("string", "Destination, IATA.", nullable=True),
    "dep_ts": _t("integer", "First-seen time, unix seconds UTC.",
                 nullable=True),
    "arr_ts": _t("integer", "Last-seen time, unix seconds UTC.",
                 nullable=True),
    "max_alt": _t("integer", "Max observed barometric altitude, feet.",
                  nullable=True),
    "callsign": _t("string", nullable=True),
})

SCH_AIRFRAME = _obj({
    "hex": _t("string"),
    "reg": _t("string", "Registration.", nullable=True),
    "type": _t("string", "ICAO type designator.", nullable=True),
    "type_name": _t("string", nullable=True),
    "category": _t("string", "Type category (narrow, wide, ...).",
                   nullable=True),
    "operator": _t("string", "Operator name.", nullable=True),
    "operator_icao": _t("string", "Observed operator: majority callsign "
                                  "prefix.", nullable=True),
    "year": _t("integer", "Build year.", nullable=True),
    "source": _t("string", "Registry row provenance.", nullable=True),
    "airline": _t("object", "Operator airline when resolved.",
                  nullable=True),
    "legs": {"description": "Observed legs, newest first, up to 200 "
                            "(the most recent when an airframe has more). "
                            "Empty array = not seen in the window; null = "
                            "the log artifact is not loaded.",
             "oneOf": [_arr(SCH_AIRFRAME_LEG), {"type": "null"}]},
    "window_days": _SCH_WINDOW,
    "coverage": _SCH_COVERAGE,
}, required=["hex", "legs", "window_days", "coverage"])

EX_AIRFRAME = {
    "hex": "76cd06", "reg": "9V-SHF", "type": "A359",
    "type_name": "Airbus A350-900", "category": "wide",
    "operator": "Singapore Airlines", "operator_icao": "SIA",
    "year": 2019, "source": "tar1090",
    "airline": {"icao": "SIA", "iata": "SQ", "name": "Singapore Airlines",
                "palette": ["#1D4886", "#FCB130"], "alliances": []},
    "legs": [{
        "date": "2026-08-27", "org": "SIN", "dst": "LHR",
        "dep_ts": 1787800000, "arr_ts": 1787845000,
        "max_alt": 41000, "callsign": "SQ322",
    }],
    "window_days": 60,
    "coverage": "observed",
}

SCH_FLIGHT = _obj({
    "callsign": _t("string"),
    "route": {"description": "[origin, ...via, destination], IATA, from "
                             "the derived routes artifact. Null when "
                             "unknown or the artifact is not loaded.",
              "oneOf": [_arr(_t("string")), {"type": "null"}]},
    "legs": {"description": "Observed legs, busiest first (top 10), with "
                            "typical local times when the inferred "
                            "timetable knows them. Null = log artifact "
                            "not loaded.",
             "oneOf": [_arr(_obj({
                 "org": _t("string"), "dst": _t("string"),
                 "flights": _t("integer", "Observed count."),
                 "days": _t("integer", "Distinct days observed."),
                 "last": _t("string", "Last date observed, YYYY-MM-DD."),
                 "dep": _t("string", "Typical departure, HH:MM local at "
                                     "origin.", nullable=True),
                 "arr": _t("string", "Typical arrival, HH:MM local at "
                                     "destination.", nullable=True),
                 "type": _t("string", "Dominant type.", nullable=True),
             })), {"type": "null"}]},
    "aircraft": {"description": "Airframes flying it, busiest first "
                                "(top 8). Null = log artifact not loaded.",
                 "oneOf": [_arr(_obj({
                     "hex": _t("string"),
                     "reg": _t("string", nullable=True),
                     "type": _t("string", nullable=True),
                     "flights": _t("integer"),
                 })), {"type": "null"}]},
    "recent": {"description": "Latest operations, newest first (up to "
                              "10). Null = log artifact not loaded.",
               "oneOf": [_arr(_obj({
                   "date": _t("string"),
                   "org": _t("string"), "dst": _t("string"),
                   "dep_ts": _t("integer", nullable=True),
                   "arr_ts": _t("integer", nullable=True),
               })), {"type": "null"}]},
    "window_days": _SCH_WINDOW,
    "coverage": _SCH_COVERAGE,
}, required=["callsign", "route", "legs", "aircraft", "recent",
             "window_days", "coverage"])

EX_FLIGHT = {
    "callsign": "SQ322",
    "route": ["SIN", "LHR"],
    "legs": [{"org": "SIN", "dst": "LHR", "flights": 12, "days": 7,
              "last": "2026-08-27", "dep": "09:00", "arr": "15:10",
              "type": "A359"}],
    "aircraft": [{"hex": "76cd06", "reg": "9V-SHF", "type": "A359",
                  "flights": 8}],
    "recent": [{"date": "2026-08-27", "org": "SIN", "dst": "LHR",
                "dep_ts": 1787800000, "arr_ts": 1787845000}],
    "window_days": 60,
    "coverage": "observed",
}

SCH_AIRPORT = _obj({
    "iata": _t("string", nullable=True),
    "ident": _t("string", "ICAO ident.", nullable=True),
    "name": _t("string", nullable=True),
    "kind": _t("string", "OurAirports kind (large_airport, ...).",
               nullable=True),
    "lat": _t("number", nullable=True),
    "lon": _t("number", nullable=True),
    "iso_country": _t("string", nullable=True),
    "municipality": _t("string", nullable=True),
    "tz": _t("string", "Olson timezone.", nullable=True),
    "observed": {"description": "Totals from the flight log. Null = log "
                                "artifact not loaded. Circuits excluded.",
                 "oneOf": [_obj({
                     "departures": _t("integer"),
                     "destinations": _t("integer"),
                     "tails": _t("integer", "Distinct airframes."),
                     "days_observed": _t("integer"),
                     "routes": _arr(_obj({
                         "dst": _t("string", "Destination, IATA."),
                         "flights": _t("integer"),
                         "days": _t("integer"),
                     }), description="Busiest routes, top 15."),
                 }), {"type": "null"}]},
    "board": _arr(_obj({
        "flight": _t("string", "Marketed number when known, else the "
                               "callsign."),
        "dst": _t("string", nullable=True),
        "dep": _t("string", "HH:MM, local.", nullable=True),
        "arr": _t("string", "HH:MM, local at destination.", nullable=True),
        "type": _t("string", nullable=True),
        "flights": _t("integer", "Observation count behind the row."),
        "source": _t("string", "observed | published | both."),
    }), description="Inferred typical departures, local time, "
                    "dep-sorted."),
    "airlines": _arr(_obj({
        "icao": _t("string"), "flights": _t("integer"),
    }), description="Busiest airlines on the board, top 20."),
    "times": _t("string", const="local"),
    "window_days": _SCH_WINDOW,
    "coverage": _SCH_COVERAGE,
}, required=["iata", "ident", "observed", "board", "airlines", "times",
             "window_days", "coverage"])

EX_AIRPORT = {
    "iata": "SIN", "ident": "WSSS", "name": "Singapore Changi",
    "kind": "large_airport", "lat": 1.35019, "lon": 103.994,
    "iso_country": "SG", "municipality": "Singapore",
    "tz": "Asia/Singapore",
    "observed": {"departures": 120, "destinations": 40, "tails": 80,
                 "days_observed": 60,
                 "routes": [{"dst": "LHR", "flights": 14, "days": 7}]},
    "board": [{"flight": "SQ322", "dst": "LHR", "dep": "09:00",
               "arr": "15:10", "type": "A359", "flights": 12,
               "source": "observed"}],
    "airlines": [{"icao": "SIA", "flights": 90}],
    "times": "local", "window_days": 60, "coverage": "observed",
}

# ---- stations -------------------------------------------------------

SCH_STATIONS = _obj({
    "stations": _arr(_obj({
        "id": _t("string", "Public id, fp-<10 hex>. Reveals no UUID bits."),
        "label": _t("string", "Operator-set display label.", nullable=True),
        "coarse_lat": _t("number", "Coverage midpoint, rounded ~11 km.",
                         nullable=True),
        "coarse_lon": _t("number", nullable=True),
        "first_seen": _t("string", "ISO 8601 UTC."),
        "last_seen": _t("string", "ISO 8601 UTC."),
        "online": _t("boolean"),
    }, required=["id", "first_seen", "last_seen", "online"])),
}, required=["stations"])

EX_STATIONS = {
    "stations": [{
        "id": "fp-a1b2c3d4e5", "label": None,
        "coarse_lat": 1.4, "coarse_lon": 103.8,
        "first_seen": "2026-08-01T00:00:00+00:00",
        "last_seen": "2026-08-29T12:00:00+00:00",
        "online": True,
    }],
}

SCH_STATION = _obj({
    "id": _t("string"),
    "online": _t("boolean"),
    "connected_since": _t("string", "ISO 8601 UTC. Null when offline.",
                          nullable=True),
    "messages_per_s": _t("number", nullable=True),
    "positions_per_s": _t("number", nullable=True),
    "kbit_s": _t("number", nullable=True),
    "rtt_ms": _t("number", nullable=True),
    "positions_total": _t("integer"),
    "aircraft_seen": _t("integer", "Aircraft this station sees right now. "
                                   "Null when unavailable.", nullable=True),
    "first_seen": _t("string"),
    "last_seen": _t("string"),
    "recent_sessions": _arr(_obj({
        "started_at": _t("string"),
        "ended_at": _t("string", "Null while open.", nullable=True),
        "peak_messages_per_s": _t("number"),
        "positions_total": _t("integer"),
    })),
}, required=["id", "online", "positions_total", "first_seen", "last_seen",
             "recent_sessions"])

EX_STATION = {
    "id": "fp-a1b2c3d4e5", "online": True,
    "connected_since": None, "messages_per_s": 0.426,
    "positions_per_s": 0.109, "kbit_s": 0.09, "rtt_ms": 16,
    "positions_total": 204, "aircraft_seen": 3,
    "first_seen": "2026-08-01T00:00:00+00:00",
    "last_seen": "2026-08-29T12:00:00+00:00",
    "recent_sessions": [{
        "started_at": "2026-08-29T00:00:00+00:00",
        "ended_at": None, "peak_messages_per_s": 0.5,
        "positions_total": 204,
    }],
}

# ---- reference ------------------------------------------------------

SCH_AIRLINE = _obj({
    "icao": _t("string"),
    "iata": _t("string", nullable=True),
    "name": _t("string"),
    "palette": _arr(_t("string"), description="Brand hexes, primary first."),
    "alliances": _arr(_t("object"), description="Sourced memberships."),
    "n_routes": _t("integer", "Observed routes. A lower bound."),
    "n_countries": _t("integer", "Countries those routes touch."),
}, required=["icao", "name", "palette", "alliances"])

EX_AIRLINE = {
    "icao": "SIA", "iata": "SQ", "name": "Singapore Airlines",
    "palette": ["#1D4886", "#FCB130"], "alliances": [],
    "n_routes": 40,
}

EX_AIRLINES = {"airlines": [EX_AIRLINE | {"n_countries": 32}]}

SCH_AIRLINES = _obj({"airlines": _arr(SCH_AIRLINE)}, required=["airlines"])

SCH_TYPE = _obj({
    "designator": _t("string"),
    "name": _t("string"),
    "category": _t("string", nullable=True),
}, required=["designator", "name", "category"])

EX_TYPE = {
    "designator": "A359", "name": "Airbus A350-900", "category": "wide",
}

EX_AIRLINE_ROUTES = {
    "icao": "SIA", "source": "flightlog",
    "airports": {"SIN": {"lat": 1.35019, "lon": 103.994,
                         "name": "Singapore Changi", "iso_country": "SG",
                         "tz": "Asia/Singapore"}},
    "legs": [{"org": "SIN", "dst": "SYD", "n": 7, "days": 7,
              "per_week": 7.0, "avg_min": 472,
              "aircraft": [{"type": "A388", "name": "Airbus A380-800",
                            "n": 4}]}],
}

EX_LEG = {
    "icao": "SIA", "org": "SIN", "dst": "SYD", "flights": 7, "days": 7,
    "airframes": [{"hex": "76cd01", "reg": "9V-SHA", "type": "A359",
                   "flights": 7}],
}

EX_SCHEDULE = {
    "icao": "SIA", "org": "SIN", "dst": "SYD",
    "departures": [{"callsign": "SIA322", "flight": "SQ322",
                    "source": "observed", "org": "SIN", "dst": "SYD",
                    "dep": "09:30", "dep_min": 570, "arr": "18:40",
                    "arr_min": 1120, "type": "A359",
                    "type_name": "Airbus A350-900", "n_flights": 30}],
}

EX_COUNTRIES = {
    "icao": "SIA",
    "countries": [{"iso_country": "SG", "n_routes": 40}],
}

EX_FLEET = {
    "icao": "SIA", "n_airframes": 3,
    "fleet": [{"type": "A359", "type_name": "Airbus A350-900", "count": 2}],
    "unknown_type": 0,
}

EX_FLEET_TYPE = {
    "icao": "SIA", "type": "A359", "type_name": "Airbus A350-900",
    "airframes": [{"hex": "76cd01", "reg": "9V-SHA"}],
}

EX_ALLIANCES = {"alliances": [{
    "slug": "star-alliance", "name": "Star Alliance",
    "website_url": "https://www.staralliance.com",
    "logo_asset_url": None,
    "source_url": "https://www.staralliance.com/members",
    "source_checked_at": "2026-08-29",
    "n_members": 25, "n_members_observed": 20, "n_airlines": 26,
    "n_routes": 4200, "n_legs": 3100, "n_flights": 90000,
    "n_countries": 120, "n_airframes": 2400,
}]}

# ---- meta -----------------------------------------------------------

SCH_INDEX = _obj({
    "name": _t("string"),
    "docs": _t("string", "Human documentation."),
    "openapi": _t("string", "Machine-readable spec path."),
    "swagger": _t("string", "Interactive spec UI path."),
    "source": _t("string", "Public repository (map client and API service)."),
    "terms": _t("string"),
    "attribution": _t("string", "The ODbL credit line."),
    "feed": _t("string", "Where to point an antenna."),
}, required=["name", "docs", "openapi", "terms", "attribution", "feed"])

SCH_HEALTHZ = _obj({"ok": _t("boolean")}, required=["ok"])

EX_INDEX = {
    "name": "FlightPortrait network API",
    "docs": "https://docs.flightportrait.com/api/reference",
    "openapi": "/openapi.json",
    "swagger": "/docs",
    "source": "https://github.com/flightportrait/network",
    "terms": "https://flightportrait.com/network/terms",
    "attribution": "Data (c) FlightPortrait network feeders, ODbL 1.0",
    "feed": "feed.flightportrait.com:30004 (beast_reduce_plus_out)",
}

EX_HEALTHZ = {"ok": True}
