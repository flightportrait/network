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
    {"name": "Contributions", "description": "What observation could not "
                                             "settle, and the community "
                                             "answers to it."},
    {"name": "Meta", "description": "Index and health."},
]

DESCRIPTION = (
    "Open data from the FlightPortrait receiver network. No API key.\n"
    "\n"
    "**Stability.** Operations marked `x-stability: stable` only ever gain "
    "fields; names and types are frozen. Operations marked `x-stability: "
    "map` exist for the first-party map and can change with it. "
    "Operations marked `x-stability: candidate` are proposed for the "
    "stable tier: their shape may still change before it is frozen.\n"
    "\n"
    "**Field dialects.** `/v1/aircraft` and `/v2/point` pass readsb's wire "
    "fields through unchanged (`hex`, `t`, `r`, `gs`, ...) so ecosystem "
    "tooling works as-is. Every other resource uses full words: `reg`, "
    "`type`, `org`, `dst`. Airport codes in history resources are IATA; "
    "`/v1/airports/{code}` accepts IATA or ICAO. Event times are unix "
    "seconds UTC; registry timestamps are ISO 8601 UTC; board and schedule "
    "times are HH:MM in the origin airport's local time.\n"
    "\n"
    "**Data honesty.** History is observation, never an official registry. "
    "Gaps mean the network's sources did not hear it, nothing more. "
    "Responses carry `coverage: \"observed\"` as a reminder. The one "
    "exception is the airport departures board, whose rows may be inferred "
    "from published timetables — each board row carries its own `source` "
    "(observed / published / both); a published row is not a receiver "
    "observation. Likewise a flight's `route_source`: `observed` when "
    "both ends were seen, `observed+catalog` when the community supplied "
    "the end coverage never reached, `catalog` when it supplied both. "
    "Catalog answers are checked against observation and reviewed before "
    "they are served, and observation outranks them whenever it speaks. "
    "This API is read-only; answers go to the contribution door at "
    "contribute.flightportrait.com.\n"
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
    "Data is ODbL 1.0. Credit \"FlightPortrait network feeders\" and link "
    "the credits page, which lists every source the data draws on and "
    "the credit each one asks for; republishing carries them too.\n"
    "Credits: https://flightportrait.com/network/credits.html\n"
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


def _weekdays():
    return _arr(_obj({
        "day": _t("string", "Mon, Tue, Wed, Thu, Fri, Sat or Sun."),
        "dep": _t("string", "HH:MM, local at origin.", nullable=True),
        "arr": _t("string", "HH:MM, local at destination.", nullable=True),
    }), description="Present only when the flight keeps another slot "
                    "altogether (more than 30 minutes from dep) on some "
                    "weekdays, read on the origin's calendar: those "
                    "days' own times, Monday first.")


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
R301_SEARCH = {301: {
    "description": "The query in another spelling than its canonical one "
                   "(trimmed, one space between words, uppercase): "
                   "Location is this URL with q canonical, every other "
                   "parameter kept. Cached like the answer.",
    "headers": {"Location": {"schema": {"type": "string"},
                             "example": "/v1/search?q=SQ%20322"}},
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


def ok(example, *extra, schema=None, status=200):
    content = {"application/json": {"example": example}}
    if schema is not None:
        content["application/json"]["schema"] = schema
    label = "Accepted" if status == 202 else "OK"
    responses = {status: {"description": label, "content": content}}
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
    "archive_through": _t("string", "Newest day in the flight-history "
                                    "archive, ISO date. Null while the "
                                    "archive is unavailable.", nullable=True),
}, required=["aircraft_count", "aircraft_with_pos", "station_count",
             "generated_at"])

EX_NOW = {
    "aircraft_count": 7,
    "aircraft_with_pos": 5,
    "station_count": 1,
    "generated_at": 1787924061.0,
    "archive_through": "2026-09-06",
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
    "baro_rate": _t("integer", "Vertical rate, feet per minute, "
                               "positive climbing."),
    "emergency": _t("string", "Transponder emergency state: none, "
                              "general, lifeguard, minfuel, nordo, "
                              "unlawful, downed, reserved."),
}, required=["hex"],
    description="readsb passthrough dialect. Optional fields are absent "
                "when unknown, never null.")

SCH_AIRCRAFT = _obj({
    "generated_at": _t("number", "Snapshot time, unix seconds UTC."),
    "total": _t("integer", "Aircraft in the whole snapshot, before any "
                           "bbox filter."),
    "with_position": _t("integer", "Of those, aircraft with a position "
                                   "fix."),
    "aircraft": _arr(SCH_AIRCRAFT_ITEM),
}, required=["generated_at", "total", "with_position", "aircraft"])

EX_AIRCRAFT = {
    "generated_at": 1787924061.0,
    "total": 191,
    "with_position": 184,
    "aircraft": [{
        "hex": "76cd06", "flight": "SIA123", "t": "A359", "r": "9V-SHF",
        "lat": 1.5, "lon": 103.8, "alt_baro": 6325, "gs": 285.9,
        "track": 85.4, "category": "A5", "squawk": "2136",
        "seen": 0.2, "seen_pos": 1.1, "baro_rate": 1856,
        "emergency": "none",
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
    "departure": {"description": "When and where this flight took off, "
                                 "from the day's trace: present when the "
                                 "network heard the take-off (from the "
                                 "ground or below 5,000 ft). Null when "
                                 "first heard aloft.",
                  "oneOf": [_obj({"at": _t("number", "Unix seconds UTC."),
                                  "lat": _t("number"), "lon": _t("number"),
                                  "alt_ft": _t("number")}), {"type": "null"}]},
    "arrival": {"description": "When and where it landed, when the trace "
                               "ends on the ground.",
                "oneOf": [_obj({"at": _t("number", "Unix seconds UTC."),
                                "lat": _t("number"), "lon": _t("number")}),
                          {"type": "null"}]},
}, required=["hex", "points"])

EX_TRACE = {
    "hex": "76cd06",
    "points": [
        [1787924000.0, 1.48, 103.7, 6000, 84.0],
        [1787924061.0, 1.50, 103.8, 6325, 85.4],
    ],
    "departure": {"at": 1787923500.0, "lat": 1.36, "lon": 103.99, "alt_ft": 425},
    "arrival": None,
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
    "country": _t("string", "State of registration, ISO 3166-1 alpha-2, "
                            "from the ICAO address block the hex belongs "
                            "to. Null for unallocated and ICAO blocks.",
                  nullable=True),
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
    "history": _t("object", "The lifetime record behind this hex: "
                            "airframe_id, msn, built_year, manufacturer, "
                            "registry (what a registry states, newest "
                            "per field: model, engine, certificate_date, "
                            "airworthiness_date, registry_status, owner, "
                            "registration, source; null without one), "
                            "first_observed, "
                            "last_observed, and dated spells (hexes, "
                            "registrations, operators; each with from, "
                            "to, legs, source) plus public events, newest "
                            "first (kind, at, lat, lon, detail, source). "
                            "Observed spells and events come from the "
                            "network's own evidence and say what it saw: "
                            "first_observed is when the network first "
                            "heard the airframe, not a delivery; "
                            "not_observed is our silence, not storage. "
                            "Null when no record exists.", nullable=True),
}, required=["hex", "legs", "window_days", "coverage"])

EX_AIRFRAME = {
    "hex": "76cd06", "reg": "9V-SHF", "type": "A359",
    "type_name": "Airbus A350-900", "category": "wide",
    "operator": "Singapore Airlines", "operator_icao": "SIA",
    "year": 2019, "country": "SG", "source": "tar1090",
    "airline": {"icao": "SIA", "iata": "SQ", "name": "Singapore Airlines",
                "palette": ["#1D4886", "#FCB130"], "alliances": []},
    "legs": [{
        "date": "2026-08-27", "org": "SIN", "dst": "LHR",
        "dep_ts": 1787800000, "arr_ts": 1787845000,
        "max_alt": 41000, "callsign": "SQ322",
    }],
    "window_days": 60,
    "coverage": "observed",
    "history": {
        "airframe_id": 4211, "msn": None, "built_year": None,
        "manufacturer": None, "registry": None,
        "first_observed": "2025-08-28", "last_observed": "2026-09-21",
        "hexes": [{"hex": "76cd06", "from": "2025-08-28",
                   "to": "2026-09-21", "legs": 812, "source": "observed"}],
        "registrations": [{"reg": "9V-SHF", "from": "2025-08-28",
                           "to": "2026-09-21", "legs": 812,
                           "source": "observed"}],
        "operators": [{"icao": "SIA", "name": "Singapore Airlines",
                       "from": "2025-08-28", "to": "2026-09-21",
                       "legs": 809, "source": "observed"}],
        "events": [{"kind": "not_observed", "at": "2026-02-03T00:00:00+00:00",
                    "lat": None, "lon": None,
                    "detail": {"last_seen": "2026-02-02",
                               "seen_again": "2026-03-10", "days": 35},
                    "source": "observed"}],
    },
}

SCH_ESTIMATED = _obj({
    "generated_at": _t("number", "Unix seconds the estimates are for."),
    "method": _t("string", "How positions are estimated."),
    "accuracy": _t("object", "The latest night's measured accuracy: day, "
                             "n, median_km, by_gap_min.", nullable=True),
    "aircraft": {"type": "array", "description": "Estimated aircraft; "
                 "every entry is an estimate, never an observation.",
                 "items": {"type": "object"}},
}, required=["generated_at", "method", "aircraft"])

EX_ESTIMATED = {
    "generated_at": 1790140000.0, "method": "converge-to-destination",
    "accuracy": {"day": "2026-09-22", "n": 208, "median_km": 2.3,
                 "by_gap_min": {"5-15": {"n": 153, "median_km": 1.4},
                                "60-120": {"n": 43, "median_km": 76.9}}},
    "aircraft": [{
        "hex": "4ca8e4", "flight": "RYR1153", "t": "B738", "r": "9H-QDS",
        "category": "A3", "lat": 43.1021, "lon": 11.9403, "track": 312.4,
        "alt_baro": 36000, "gs": 452.0, "estimated": True,
        "last_seen": {"at": 1790139412.0, "lat": 42.2, "lon": 13.1},
        "destination": "PSA", "eta": 1790140700,
    }],
}

EX_AIRLINE_AIRFRAMES = {
    "icao": "TVS", "as_of": "2026-09-22", "current_days": 60,
    "airframes": [{
        "hex": "49d283", "reg": "OK-TVY", "type": "B738", "country": "CZ",
        "built_year": None, "msn": None, "since": "2026-05-08",
        "since_first_seen": False, "last_seen": "2026-09-21",
        "airlines": 3, "notable": 0,
    }],
}

SCH_ROUTES = _obj({
    "routes": {"description": "Callsign (upper case) to [origin, ...via, "
                              "destination] IATA, or null when unknown. "
                              "Every requested callsign is a key.",
               "type": "object",
               "additionalProperties": {
                   "oneOf": [_arr(_t("string")), {"type": "null"}]}},
})

SCH_FLIGHT = _obj({
    "callsign": _t("string", "The operating callsign the record is for."),
    "marketed": _t("string", "The marketed number the request used "
                             "when it differed (BA272 for BAW272).",
                   nullable=True),
    "route": {"description": "[origin, ...via, destination], IATA, from "
                             "the derived routes artifact, else from the "
                             "community catalog. Null when unknown or "
                             "the artifact is not loaded.",
              "oneOf": [_arr(_t("string")), {"type": "null"}]},
    "route_source": _t("string", "observed, observed+catalog, or catalog. "
                                 "Null when route is null.", nullable=True),
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
                 "times": _t("string", "Where dep/arr come from: observed "
                                       "(inferred from what flew), "
                                       "published (an airport's board), "
                                       "or both.", nullable=True),
                 "flight": _t("string", "The marketed flight number when "
                                        "a board named it (e.g. LH996 "
                                        "for callsign DLH8AE).",
                              nullable=True),
                 "weekdays": _weekdays(),
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

EX_ROUTES = {"routes": {"SQ322": ["SIN", "LHR"], "BAW9": ["LHR", "SIN"],
                        "ZZZZ9": None}}

EX_FLIGHT = {
    "callsign": "SQ322",
    "route": ["SIN", "LHR"],
    "route_source": "observed",
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
    "role": _t("string", "commercial, general, military, or closed.",
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
    "today": {"description": "Today's board as the airport publishes it, "
                             "in the airport's local day. Null when no "
                             "published board is held for it.",
              "oneOf": [_obj({
                  "day": _t("string", "YYYY-MM-DD, local."),
                  "source": _t("string", "published"),
                  "departures": _arr(_obj({
                      "flight": _t("string", "Marketed number."),
                      "dst": _t("string", nullable=True),
                      "dep": _t("string", "HH:MM, local."),
                      "callsign": _t("string", "The observed service that "
                                               "carries the number, when "
                                               "known.", nullable=True),
                      "type": _t("string", nullable=True),
                  })),
                  "arrivals": _arr(_obj({
                      "flight": _t("string", "Marketed number."),
                      "org": _t("string", nullable=True),
                      "arr": _t("string", "HH:MM, local."),
                      "callsign": _t("string", nullable=True),
                      "type": _t("string", nullable=True),
                  })),
              }), {"type": "null"}]},
    "board": _arr(_obj({
        "flight": _t("string", "Marketed number when known, else the "
                               "callsign."),
        "dst": _t("string", nullable=True),
        "dep": _t("string", "HH:MM, local.", nullable=True),
        "arr": _t("string", "HH:MM, local at destination.", nullable=True),
        "type": _t("string", nullable=True),
        "flights": _t("integer", "Observation count behind the row."),
        "source": _t("string", "Provenance of THIS row: observed (from "
                               "receivers), published (from a timetable), "
                               "or both. Unlike the observed stats, a "
                               "published row is not a receiver "
                               "observation."),
        "weekdays": _weekdays(),
    }), description="Inferred typical departures, local time, dep-sorted. "
                    "Rows are a mix of observation and published timetable "
                    "data; check each row's source."),
    "arrivals": _arr(_obj({
        "flight": _t("string", "Marketed number when known, else the "
                               "callsign."),
        "org": _t("string", "Origin, IATA.", nullable=True),
        "dep": _t("string", "HH:MM, local at origin.", nullable=True),
        "arr": _t("string", "HH:MM, local at this airport.", nullable=True),
        "type": _t("string", nullable=True),
        "flights": _t("integer", "Observation count behind the row."),
        "source": _t("string", "observed, published, or both."),
        "weekdays": _weekdays(),
    }), description="Inferred typical arrivals, this airport's local "
                    "time, arr-sorted. Same provenance rules as board."),
    "airlines": _arr(_obj({
        "icao": _t("string"), "flights": _t("integer"),
    }), description="Busiest airlines on the board, top 20."),
    "times": _t("string", const="local"),
    "window_days": _SCH_WINDOW,
    "coverage": _SCH_COVERAGE,
}, required=["iata", "ident", "observed", "board", "arrivals", "airlines",
             "times", "window_days", "coverage"])

EX_AIRPORT = {
    "iata": "SIN", "ident": "WSSS", "name": "Singapore Changi",
    "kind": "large_airport", "lat": 1.35019, "lon": 103.994,
    "iso_country": "SG", "municipality": "Singapore",
    "tz": "Asia/Singapore",
    "observed": {"departures": 120, "destinations": 40, "tails": 80,
                 "days_observed": 60,
                 "routes": [{"dst": "LHR", "flights": 14, "days": 7}]},
    "today": {"day": "2026-09-23", "source": "published",
              "departures": [{"flight": "SQ322", "dst": "LHR", "dep": "09:00",
                              "callsign": "SIA322", "type": "A359"}],
              "arrivals": [{"flight": "SQ317", "org": "LHR", "arr": "06:55",
                            "callsign": "SIA317", "type": "A359"}]},
    "board": [{"flight": "SQ322", "dst": "LHR", "dep": "09:00",
               "arr": "15:10", "type": "A359", "flights": 12,
               "source": "observed"}],
    "arrivals": [{"flight": "SQ317", "org": "LHR", "dep": "11:00",
                  "arr": "06:55", "type": "A359", "flights": 11,
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

SCH_SEARCH = _obj({
    "q": _t("string", "The query as searched, trimmed and uppercased."),
    "results": _arr(_obj({
        "kind": _t("string", "aircraft, flight, airport or airline."),
        "id": _t("string", "What to open: hex, callsign, airport code "
                           "or airline ICAO."),
        "label": _t("string", "The line to show."),
        "detail": _t("string", "A second line: type and operator, route "
                               "count, city and country, IATA.",
                     nullable=True),
        "score": _t("number", "Rank: 100 an exact code, 60 a prefix, 40 a "
                              "word start, 20 a near miss, plus a little "
                              "for traffic. The list is sorted by it."),
    }, required=["kind", "id", "label", "detail", "score"])),
}, required=["q", "results"])

EX_SEARCH = {
    "q": "9V-SH",
    "results": [
        {"kind": "aircraft", "id": "76cd01", "label": "9V-SHA",
         "detail": "Airbus A350-900 · Singapore Airlines", "score": 60},
        {"kind": "aircraft", "id": "76cd02", "label": "9V-SHB",
         "detail": "Airbus A350-900 · Singapore Airlines", "score": 60},
    ],
}

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
    "window_days": 400,
    "airframes": [{"hex": "76cd01", "reg": "9V-SHA", "legs": 212,
                   "last_date": "2026-08-27", "last_org": "SIN",
                   "last_dst": "LHR", "where": "LHR",
                   "top_route": ["SIN", "LHR", 41]}],
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
    "llms": _t("string", "An llms.txt for language models and agents."),
    "openapi": _t("string", "Machine-readable spec path."),
    "swagger": _t("string", "Interactive spec UI path."),
    "source": _t("string", "Public repository (map client and API service)."),
    "terms": _t("string"),
    "attribution": _t("string", "The short credit line; the credits page "
                                "holds each source's own credit."),
    "credits": _t("string", "Every source the data draws on, with its "
                            "licence and credit."),
    "feed": _t("string", "Where to point an antenna."),
}, required=["name", "docs", "openapi", "terms", "attribution", "credits",
             "feed"])

SCH_HEALTHZ = _obj({"ok": _t("boolean")}, required=["ok"])

EX_INDEX = {
    "name": "FlightPortrait network API",
    "docs": "https://docs.flightportrait.com/api/reference",
    "llms": "/llms.txt",
    "openapi": "/openapi.json",
    "swagger": "/docs",
    "source": "https://github.com/flightportrait/network",
    "terms": "https://flightportrait.com/network/terms",
    "attribution": "Data (c) FlightPortrait network feeders and credited "
                   "sources, ODbL 1.0",
    "credits": "https://flightportrait.com/network/credits.html",
    "feed": "feed.flightportrait.com:30004 (beast_reduce_plus_out)",
}

EX_HEALTHZ = {"ok": True}


# ---- contributions ----------------------------------------------------

_SCH_GAP_ROW = _obj({
    "callsign": _t("string"),
    "side": _t("string", "The missing end: origin or dest."),
    "known": _t("string", "The settled end, IATA."),
    "hint": _t("string", "The missing end's leading code when observation "
                         "saw it too rarely to settle it.", nullable=True),
    "chain": {"description": "Every known stop in order when the callsign "
                             "is a chain with one end unseen; the missing "
                             "end goes before or after it. Null for a "
                             "single leg.",
              "oneOf": [_arr(_t("string")), {"type": "null"}]},
    "type": _t("string", "Dominant aircraft type on the leg.",
               nullable=True),
    "n_recent": _t("integer", "Sightings in the last 90 days."),
    "last_seen": _t("string", "Last date observed, YYYY-MM-DD."),
    "last_heard": {"description": "Where the most recent truncated leg "
                                  "was last heard, with its track in "
                                  "degrees true. Null when unknown.",
                   "oneOf": [_obj({"lat": _t("number"),
                                   "lon": _t("number"),
                                   "track": _t("integer", nullable=True)}),
                             {"type": "null"}]},
    "rotation_km": _t("integer", "How far the missing end is, from the "
                                 "time the airframe takes to come back. "
                                 "Null until a few rotations were seen.",
                      nullable=True),
    "suggested": _arr(_t("string"), description="Airports the evidence "
                      "allows for the missing end, best first: at the "
                      "rotation's distance, within the type's range, "
                      "along the last heard track, ranked by how much "
                      "the airline is seen flying there. Empty until the "
                      "rotation is known."),
})

SCH_GAPS = _obj({
    "total": _t("integer", "Rows matching the filters."),
    "offset": _t("integer"),
    "gaps": _arr(_SCH_GAP_ROW),
    "coverage": _SCH_COVERAGE,
}, required=["total", "offset", "gaps", "coverage"])

EX_GAPS = {
    "total": 1, "offset": 0,
    "gaps": [{"callsign": "SIA842", "side": "dest", "known": "SIN",
              "hint": None, "chain": None, "type": "B78X", "n_recent": 13,
              "last_seen": "2026-09-06",
              "last_heard": {"lat": 12.41, "lon": 106.92, "track": 21},
              "rotation_km": 3150, "suggested": ["TFU", "CTU"]}],
    "coverage": "observed",
}

SCH_GAP = _obj(dict(_SCH_GAP_ROW["properties"], **{
    "catalog": {"description": "The community answer in force, if any: "
                               "the whole route, IATA.",
                "oneOf": [_obj({"route": _arr(_t("string")),
                                "valid_from": _t("string")}),
                          {"type": "null"}]},
    "answers": _arr(_obj({
        "origin": _t("string"), "dest": _t("string"),
        "status": _t("string", "pending or approved."),
        "verdict": _t("string", "corroborated, contradicted, unverified, "
                                "or contested.", nullable=True),
    }), description="Answers on file, oldest first, rejected ones left out."),
}))

EX_GAP = dict(EX_GAPS["gaps"][0], catalog=None, answers=[])

SCH_CONTRIBUTORS = _obj({
    "answers": _t("integer", "Approved claims, all contributors."),
    "contributors": _arr(_obj({
        "handle": _t("string"),
        "answers": _t("integer", "Approved claims stood behind."),
        "latest": _t("string", "Date of the latest, YYYY-MM-DD.",
                     nullable=True),
    }), description="Most answers first, top 200."),
}, required=["answers", "contributors"])

EX_CONTRIBUTORS = {
    "answers": 41,
    "contributors": [{"handle": "spotter_sg", "answers": 23,
                      "latest": "2026-09-14"}],
}


# ---- /v2/search (served by networkd only) ----------------------------
# networkd answers /v2/search from its own search index; this service
# does not. The operation is documented here so the one OpenAPI
# document (which networkd serves) describes it; main.py adds it to the
# generated paths.

CANDIDATE = {"x-stability": "candidate"}

_SITE = "https://flightportrait.com/network/"

_V2_AIRLINE_REF = _obj({
    "icao": _t("string", "ICAO designator, e.g. SIA."),
    "iata": _t("string", "IATA code, e.g. SQ.", nullable=True),
    "name": _t("string", "Airline name."),
}, required=["icao", "iata", "name"])

_V2_LEG = _obj({
    "org": _t("string", "Origin airport code (IATA, else ICAO)."),
    "dst": _t("string", "Destination airport code (IATA, else ICAO)."),
    "dep": _t("string", "Usual departure, HH:MM local time at org (the "
                        "zone in dep_tz).", nullable=True),
    "arr": _t("string", "Usual arrival, HH:MM local time at dst (the zone "
                        "in arr_tz).", nullable=True),
    "dep_tz": _t("string", "IANA time zone of org, e.g. Asia/Singapore.",
                 nullable=True),
    "arr_tz": _t("string", "IANA time zone of dst, e.g. Europe/London.",
                 nullable=True),
    "block_min": _t("integer", "Minutes from dep to arr, both zones "
                               "applied (on the index's date).",
                    nullable=True),
    "type": _t("string", "Usual aircraft type (ICAO designator).",
               nullable=True),
    "times": _t("string", "Where dep and arr come from: observed (the "
                          "network watched it fly), published (an "
                          "airport's timetable), or both.", nullable=True),
    "flights": _t("integer", "Times this leg was flown in the flight "
                             "log's window (window_days)."),
}, required=["org", "dst", "dep", "arr", "dep_tz", "arr_tz", "block_min",
             "type", "times", "flights"])

_V2_SCORE = _t("number", "Rank, highest first: 1000 and up an exact code; "
                         "about 900 what the query was read as (its intent), "
                         "40 less for each further reading; 600 names "
                         "matched whole, 500 a word still being typed, "
                         "470 or 380 a typo (one or two edits), lower for "
                         "part of the words. Popularity (flights, "
                         "departures, airframes) orders within a class and "
                         "never lifts a result into the one above. Compare "
                         "scores within one answer only.")

_V2_URL = _t("string", "The result's page on the network site, to cite or "
                       "link.", format="uri")

SCH_V2_FLIGHT = _obj({
    "kind": _t("string", "flight", enum=["flight"]),
    "id": _t("string", "Stable id: the ATC callsign (ICAO), e.g. SIA322. "
                       "Opens /v1/flights/{callsign}."),
    "callsign": _t("string", "The ATC callsign, e.g. SIA322."),
    "flight": _t("string", "The marketed flight number (IATA), e.g. SQ322: "
                           "the one a timetable named, else the airline's "
                           "IATA code before the callsign's number.",
                 nullable=True),
    "airline": {"description": "The operating airline.",
                "oneOf": [_V2_AIRLINE_REF, {"type": "null"}]},
    "route": _arr(_t("string"), "Airport codes in flying order, origin "
                                "first."),
    "cities": _arr(_t("string", nullable=True), "The city of each code in "
                                                "route, same order."),
    "legs": _arr(_V2_LEG, "Each leg, in flying order."),
    "flights": _t("integer", "Times the flight was flown in the window "
                             "(all legs)."),
    "window_days": _t("integer", "Days of flight log behind flights and "
                                 "last_seen.", nullable=True),
    "last_seen": _t("string", "Date it was last seen, YYYY-MM-DD (UTC).",
                    nullable=True),
    "url": _V2_URL,
    "score": _V2_SCORE,
}, required=["kind", "id", "callsign", "flight", "airline", "route",
             "cities", "legs", "flights", "window_days", "last_seen", "url",
             "score"], description="A flight number and what the network "
                                   "knows of how it usually flies.")

SCH_V2_AIRLINE = _obj({
    "kind": _t("string", "airline", enum=["airline"]),
    "id": _t("string", "Stable id: the ICAO designator. Opens "
                       "/v1/airlines/{icao}."),
    "icao": _t("string", "ICAO designator."),
    "iata": _t("string", "IATA code.", nullable=True),
    "name": _t("string", "Name."),
    "flights": _t("integer", "Flights of its schedule in the flight log's "
                             "window: how active it is here."),
    "url": _V2_URL,
    "score": _V2_SCORE,
}, required=["kind", "id", "icao", "iata", "name", "flights", "url", "score"])

SCH_V2_AIRPORT = _obj({
    "kind": _t("string", "airport", enum=["airport"]),
    "id": _t("string", "Stable id: the IATA code, else the ICAO/registry "
                       "ident. Opens /v1/airports/{code}."),
    "iata": _t("string", "IATA code.", nullable=True),
    "icao": _t("string", "ICAO code (the registry's ident)."),
    "name": _t("string", "Name.", nullable=True),
    "city": _t("string", "The municipality the registry gives.",
               nullable=True),
    "country": _t("string", "ISO 3166-1 alpha-2.", nullable=True),
    "tz": _t("string", "IANA time zone.", nullable=True),
    "flights": _t("integer", "Scheduled departures seen in the flight log's "
                             "window: how busy it is here."),
    "url": _V2_URL,
    "score": _V2_SCORE,
}, required=["kind", "id", "iata", "icao", "name", "city", "country", "tz",
             "flights", "url", "score"])

SCH_V2_AIRCRAFT = _obj({
    "kind": _t("string", "aircraft", enum=["aircraft"]),
    "id": _t("string", "Stable id: the ICAO 24-bit address, 6 lowercase hex "
                       "characters. Opens /v1/airframes/{hex}."),
    "hex": _t("string", "ICAO 24-bit address, lowercase."),
    "reg": _t("string", "Registration, e.g. 9V-SMA.", nullable=True),
    "type": _t("string", "ICAO type designator.", nullable=True),
    "type_name": _t("string", "Type name.", nullable=True),
    "operator": _t("string", "The airline the network watched it fly for, "
                             "else the registry's owner or operator.",
                   nullable=True),
    "operator_icao": _t("string", "That airline's ICAO designator, when "
                                  "observed.", nullable=True),
    "url": _V2_URL,
    "score": _V2_SCORE,
}, required=["kind", "id", "hex", "reg", "type", "type_name", "operator",
             "operator_icao", "url", "score"])

SCH_V2_TYPE = _obj({
    "kind": _t("string", "type", enum=["type"]),
    "id": _t("string", "Stable id: the ICAO type designator. Opens "
                       "/v1/types/{designator}."),
    "designator": _t("string", "ICAO type designator, e.g. A388."),
    "name": _t("string", "Name, e.g. Airbus A380-800.", nullable=True),
    "manufacturer": _t("string", "Manufacturer, from the name.",
                       nullable=True),
    "family": _t("string", "Family, e.g. A380, 787.", nullable=True),
    "airframes": _t("integer", "Airframes of this type in the registry."),
    "url": _t("string", "Null: the site has no page for a type yet.",
              nullable=True),
    "score": _V2_SCORE,
}, required=["kind", "id", "designator", "name", "manufacturer", "family",
             "airframes", "url", "score"])

_V2_PLACE = _obj({
    "text": _t("string", "The words as read (folded)."),
    "airports": _arr(_t("string"), "The airports they stand for, the main "
                                   "one first."),
    "how": _t("string", "code (an airport code), city (a city: every "
                        "airport serving it) or airport (an airport's "
                        "name)."),
}, required=["text", "airports", "how"])

SCH_V2_INTENT = {
    "description": "What the query was read as: the first reading that "
                   "found something. kind is flight (a flight number), "
                   "registration, route (two places), fleet (an airline and "
                   "an aircraft family), airline_place (an airline and a "
                   "place), type, city, code (a code of any kind, typed "
                   "whole) or text (names only).",
    "type": "object",
    "properties": {
        "kind": _t("string", enum=["flight", "registration", "route",
                                   "fleet", "airline_place", "type", "city",
                                   "code", "text"]),
        "number": _t("string", "flight: the number as typed, compact."),
        "callsigns": _arr(_t("string"), "flight: the callsigns and marketed "
                                        "numbers looked up."),
        "registration": _t("string", "registration: the mark without "
                                     "spaces or dashes."),
        "from": _V2_PLACE, "to": _V2_PLACE, "place": _V2_PLACE,
        "airline": _t("string", "fleet, airline_place: ICAO designator."),
        "family": _t("string", "fleet, type: the family or designator "
                               "read."),
        "types": _arr(_t("string"), "fleet, type: its designators, most "
                                    "airframes first."),
        "code": _t("string", "code: the code as looked up."),
    },
    "required": ["kind"],
}

SCH_V2_SEARCH = _obj({
    "q": _t("string", "The query as searched: trimmed, one space between "
                      "words, uppercase."),
    "intent": SCH_V2_INTENT,
    "as_of": _t("string", "When the data behind the index was taken, ISO "
                          "8601 UTC.", format="date-time"),
    "index": _t("string", "The index generation that answered, e.g. "
                          "20260926T024000Z. Answers from one generation "
                          "are consistent with each other."),
    "results": _arr({"oneOf": [SCH_V2_FLIGHT, SCH_V2_AIRLINE, SCH_V2_AIRPORT,
                               SCH_V2_AIRCRAFT, SCH_V2_TYPE],
                     "discriminator": {"propertyName": "kind"}},
                    "Typed results, best first."),
}, required=["q", "intent", "as_of", "index", "results"])

_EX_SIA = {"icao": "SIA", "iata": "SQ", "name": "Singapore Airlines"}


def _ex_flight(cs, number, airline, legs, flights, last_seen, score, cities):
    route = [legs[0]["org"]] + [l["dst"] for l in legs]
    return {"kind": "flight", "id": cs, "callsign": cs, "flight": number,
            "airline": airline, "route": route, "cities": cities,
            "legs": legs, "flights": flights, "window_days": 400,
            "last_seen": last_seen,
            "url": _SITE + "flight.html?callsign=" + cs, "score": score}


def _ex_leg(org, dst, dep, arr, dep_tz, arr_tz, block, type_, times, n):
    return {"org": org, "dst": dst, "dep": dep, "arr": arr, "dep_tz": dep_tz,
            "arr_tz": arr_tz, "block_min": block, "type": type_,
            "times": times, "flights": n}


_EX_SQ322 = _ex_flight(
    "SIA322", "SQ322", _EX_SIA,
    [_ex_leg("SIN", "LHR", "23:35", "06:25", "Asia/Singapore",
             "Europe/London", 830, "A388", "both", 361)],
    361, "2026-09-25", 1032.9, ["Singapore", "London"])
_EX_SQ308 = _ex_flight(
    "SIA308", "SQ308", _EX_SIA,
    [_ex_leg("SIN", "LHR", "09:05", "15:55", "Asia/Singapore",
             "Europe/London", 830, "A359", "observed", 352)],
    352, "2026-09-25", 945.3, ["Singapore", "London"])
_EX_BA12 = _ex_flight(
    "BAW12", "BA12", {"icao": "BAW", "iata": "BA", "name": "British Airways"},
    [_ex_leg("SIN", "LHR", "23:20", "05:40", "Asia/Singapore",
             "Europe/London", 800, "A35K", "both", 355)],
    355, "2026-09-25", 945.2, ["Singapore", "London"])
_EX_SQ317 = _ex_flight(
    "SIA317", "SQ317", _EX_SIA,
    [_ex_leg("LHR", "SIN", "11:05", "07:25", "Europe/London",
             "Asia/Singapore", 800, "A359", "observed", 340)],
    340, "2026-09-24", 925.2, ["London", "Singapore"])

EX_V2_SEARCH = {
    "flight_number": {
        "summary": "A flight number, typed with a space",
        "description": "GET /v2/search?q=SQ%20322 — its IATA number finds "
                       "the callsign that flies it; flights whose numbers "
                       "start with it follow.",
        "value": {"q": "SQ 322",
                  "intent": {"kind": "flight", "number": "SQ322",
                             "callsigns": ["SQ322", "SIA322"]},
                  "as_of": "2026-09-26T02:40:00Z",
                  "index": "20260926T024000Z",
                  "results": [_EX_SQ322]}},
    "route_in_words": {
        "summary": "A route in words, cities for their airports",
        "description": "GET /v2/search?q=SINGAPORE%20TO%20LONDON — a city "
                       "stands for every airport serving it; nonstop "
                       "flights first, then by how often they fly.",
        "value": {"q": "SINGAPORE TO LONDON",
                  "intent": {"kind": "route",
                             "from": {"text": "singapore",
                                      "airports": ["SIN"], "how": "city"},
                             "to": {"text": "london",
                                    "airports": ["LHR", "LGW", "STN", "LTN",
                                                 "LCY", "SEN"],
                                    "how": "city"}},
                  "as_of": "2026-09-26T02:40:00Z",
                  "index": "20260926T024000Z",
                  "results": [dict(_EX_SQ322, score=939.9), _EX_SQ308,
                              _EX_BA12]}},
    "airline_and_place": {
        "summary": "An airline and a place",
        "description": "GET /v2/search?q=SINGAPORE%20AIRLINES%20LONDON — "
                       "that airline's flights to and from the place.",
        "value": {"q": "SINGAPORE AIRLINES LONDON",
                  "intent": {"kind": "airline_place", "airline": "SIA",
                             "place": {"text": "london",
                                       "airports": ["LHR", "LGW", "STN",
                                                    "LTN", "LCY", "SEN"],
                                       "how": "city"}},
                  "as_of": "2026-09-26T02:40:00Z",
                  "index": "20260926T024000Z",
                  "results": [dict(_EX_SQ322, score=935.3),
                              dict(_EX_SQ308, score=935.2), _EX_SQ317]}},
    "city": {
        "summary": "A city: every airport serving it",
        "description": "GET /v2/search?q=TOKYO&kinds=airport",
        "value": {"q": "TOKYO",
                  "intent": {"kind": "city",
                             "place": {"text": "tokyo",
                                       "airports": ["HND", "NRT"],
                                       "how": "city"}},
                  "as_of": "2026-09-26T02:40:00Z",
                  "index": "20260926T024000Z",
                  "results": [
                      {"kind": "airport", "id": "HND", "iata": "HND",
                       "icao": "RJTT",
                       "name": "Tokyo Haneda International Airport",
                       "city": "Tokyo", "country": "JP", "tz": "Asia/Tokyo",
                       "flights": 50289,
                       "url": _SITE + "?airport=HND", "score": 924.7},
                      {"kind": "airport", "id": "NRT", "iata": "NRT",
                       "icao": "RJAA", "name": "Narita International Airport",
                       "city": "Narita", "country": "JP", "tz": "Asia/Tokyo",
                       "flights": 12848,
                       "url": _SITE + "?airport=NRT", "score": 921.1}]}},
    "type": {
        "summary": "An aircraft family",
        "description": "GET /v2/search?q=A380",
        "value": {"q": "A380",
                  "intent": {"kind": "type", "family": "A380",
                             "types": ["A388"]},
                  "as_of": "2026-09-26T02:40:00Z",
                  "index": "20260926T024000Z",
                  "results": [
                      {"kind": "type", "id": "A388", "designator": "A388",
                       "name": "Airbus A380-800", "manufacturer": "Airbus",
                       "family": "A380", "airframes": 261, "url": None,
                       "score": 912.4}]}},
    "registration": {
        "summary": "A registration, however typed",
        "description": "GET /v2/search?q=9V%20SMA",
        "value": {"q": "9V SMA",
                  "intent": {"kind": "registration",
                             "registration": "9VSMA"},
                  "as_of": "2026-09-26T02:40:00Z",
                  "index": "20260926T024000Z",
                  "results": [
                      {"kind": "aircraft", "id": "76cda1", "hex": "76cda1",
                       "reg": "9V-SMA", "type": "A359",
                       "type_name": "Airbus A350-900",
                       "operator": "Singapore Airlines",
                       "operator_icao": "SIA",
                       "url": _SITE + "plane.html?hex=76cda1",
                       "score": 1030.0}]}},
}

R301_SEARCH_V2 = {301: {
    "description": "A parameter in another spelling than its canonical "
                   "one (q trimmed, one space between words, uppercase; "
                   "kinds in the order flight, airline, airport, aircraft, "
                   "type; limit as a plain number; near rounded to 0.25°): "
                   "Location is this URL with them canonical, every other "
                   "parameter kept. Cached like the answer.",
    "headers": {"Location": {"schema": {"type": "string"},
                             "example": "/v2/search?q=SQ%20322"}},
}}

R503_SEARCH_V2 = {503: {
    "description": "The search index is not loaded "
                   "(artifact_unavailable). Retry-After is set. Never "
                   "answered from anything else.",
    "content": {"application/json": {
        "schema": ERROR_SCHEMA,
        "example": {"error": "artifact_unavailable",
                    "detail": "search index not loaded"}}},
}}

NETWORKD_PATHS = {"/v2/search": {"get": {
    "tags": ["Reference"],
    "summary": "Search (typed)",
    "description": (
        "One box over flights, airlines, airports, airframes and aircraft "
        "types, read before it is searched: a flight number (\"SQ322\", "
        "\"SQ 322\", \"SIA322\"), a route in words where a city stands for "
        "all its airports (\"New York to London\", \"SIN-LHR\"), an airline "
        "and a place (\"Singapore Airlines London\") or a family (\"SQ "
        "777\", \"BA A380\"), a city (\"Tokyo\": HND and NRT), a type "
        "(\"A380\", \"787\", \"Boeing 777\"), a registration however typed "
        "(\"9V SMA\", \"9VSMA\"), a hex, any code. Then names, accents and "
        "case aside, with the last word taken as still being typed; near "
        "misses (one edit for 4-5 letters, two from 6) only when nothing "
        "matched whole. Every result is typed (its fields depend on kind), "
        "with a stable id, the page to link, and flight times in local "
        "time with their zones.\n\n"
        "The answer comes from a search index rebuilt nightly from the "
        "reference snapshot; `index` and `as_of` say which. `near` "
        "(a coarse position, rounded to 0.25° before anything reads it; "
        "never stored or logged) may lift nearby airports.\n\n"
        "Rate: 600 per 600 s (bucket `search_v2`). Cache: 12 h edge. "
        "Stability: candidate for the stable tier."),
    "operationId": "search_v2",
    "parameters": [
        {"name": "q", "in": "query", "required": True,
         "description": "What was typed, 1-40 characters (at least 2 after "
                        "trimming).",
         "schema": {"type": "string", "minLength": 1, "maxLength": 40},
         "examples": {"flight": {"value": "SQ 322"},
                      "route": {"value": "SINGAPORE TO LONDON"},
                      "airline_place": {"value": "SINGAPORE AIRLINES LONDON"}}},
        {"name": "kinds", "in": "query", "required": False,
         "description": "Only these kinds, comma-separated: flight, airline, "
                        "airport, aircraft, type. Default: all.",
         "schema": {"type": "string"}, "example": "flight,airport"},
        {"name": "limit", "in": "query", "required": False,
         "description": "At most this many results, 1-50. Default 10.",
         "schema": {"type": "integer", "minimum": 1, "maximum": 50,
                    "default": 10}},
        {"name": "near", "in": "query", "required": False,
         "description": "lat,lon in degrees: a coarse position to rank by, "
                        "rounded to 0.25° (a spelling not rounded "
                        "redirects to the rounded one).",
         "schema": {"type": "string"}, "example": "1.25,103.75"},
    ],
    "responses": {
        "200": {"description": "OK", "content": {"application/json": {
            "schema": SCH_V2_SEARCH, "examples": EX_V2_SEARCH}}},
        **{str(k): v for k, v in (R301_SEARCH_V2 | R422 | R429
                                   | R503_SEARCH_V2).items()},
    },
    **CANDIDATE,
}}}
