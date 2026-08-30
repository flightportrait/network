"""Live-sky routes: the snapshot views and the ecosystem-compatible
point query. Throttle first, always; cache headers explicit on every
response so Cloudflare's behavior is decided here, not by defaults.

Everything here reads the in-memory snapshot and nothing else — the load
on readsb is the poller's constant ~0.2 req/s no matter what the public
does. /v2/point included: it filters the snapshot by distance instead of
proxying the aggregator, so its envelope stays ecosystem-shaped while its
cost stays memory-only.
"""
import math
import time

from fastapi import APIRouter, Request, Response

from . import openapi as spec
from . import ratelimit
from .errors import ApiError

router = APIRouter()

CACHE_LIVE = "public, max-age=5, s-maxage=10"
CACHE_POINT = "public, s-maxage=5"

EARTH_RADIUS_NM = 3440.065


def _fresh_snapshot(request: Request):
    snapshot = request.app.state.snapshot
    if not snapshot.fresh(request.app.state.settings.stale_after_s):
        # A dead upstream must read as an outage, never as an empty sky.
        raise ApiError(503, "stale_snapshot", "sky data unavailable",
                       headers={"Retry-After": "5"})
    return snapshot


@router.get(
    "/v1/now", tags=["Live"], summary="Counts",
    description="Aircraft and station counts from the live snapshot. "
                "503 if the snapshot is older than 60 seconds. "
                "Rate: 600 per 600 s (bucket `now`). Cache: 10 s edge, "
                "5 s browser.",
    operation_id="now",
    responses=spec.ok(spec.EX_NOW, spec.R429, spec.R503,
                      schema=spec.SCH_NOW),
    openapi_extra=spec.STABLE,
)
def now(request: Request, response: Response):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.now_rate_limit,
                       settings.rate_window_s, bucket="now")
    snapshot = _fresh_snapshot(request)
    presence = request.app.state.presence
    response.headers["Cache-Control"] = CACHE_LIVE
    return {
        "aircraft_count": snapshot.aircraft_count,
        "aircraft_with_pos": snapshot.with_pos_count,
        "station_count": (len(presence)
                          if request.app.state.presence_available else None),
        "generated_at": snapshot.generated_at,
    }


@router.get(
    "/v1/aircraft", tags=["Live"], summary="Aircraft",
    description="Aircraft the network hears right now, readsb field "
                "dialect. 503 if the snapshot is older than 60 seconds. "
                "Rate: 300 per 600 s (bucket `aircraft`). Cache: 10 s "
                "edge, 5 s browser.",
    operation_id="aircraft",
    responses=spec.ok(spec.EX_AIRCRAFT, spec.R429, spec.R503,
                      schema=spec.SCH_AIRCRAFT),
    openapi_extra=spec.STABLE,
)
def aircraft(request: Request, response: Response):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.aircraft_rate_limit,
                       settings.rate_window_s, bucket="aircraft")
    snapshot = _fresh_snapshot(request)
    response.headers["Cache-Control"] = CACHE_LIVE
    return {"generated_at": snapshot.generated_at,
            "aircraft": snapshot.aircraft}


@router.get(
    "/v1/trace/{hex}", tags=["Live"], summary="Trace",
    description="Recent positions for one aircraft, oldest first. Each "
                "point is [t, lat, lon, alt_baro, track]. Held in memory "
                "about 30 minutes; a restart forgets and trails regrow. "
                "404 if none. Rate: 600 per 600 s (bucket `trace`). "
                "Cache: 5 s edge.",
    operation_id="trace",
    responses=spec.ok(spec.EX_TRACE, spec.R429, spec.R404,
                      schema=spec.SCH_TRACE),
    openapi_extra=spec.STABLE,
)
def trace(hex: spec.Hex, request: Request, response: Response):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.trace_rate_limit,
                       settings.rate_window_s, bucket="trace")
    points = request.app.state.traces.get(hex)
    if not points:
        raise ApiError(404, "not_found", "no trace")
    response.headers["Cache-Control"] = CACHE_POINT
    # points: [t, lat, lon, alt_baro, track], oldest first
    return {"hex": hex.strip().lower(), "points": points}


def _distance_nm(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + \
        math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(a))


@router.get(
    "/v2/point/{lat}/{lon}/{radius}", tags=["Live"], summary="Point",
    description="Aircraft within radius nautical miles of a point, from "
                "the live snapshot. Radius is capped at 250. The common "
                "v2 envelope (ac, now, total) so ecosystem tooling can "
                "consume it unchanged; fields inside ac are the "
                "/v1/aircraft allowlist plus dst (distance, nm), nearest "
                "first. 503 if the snapshot is older than 60 seconds. "
                "Rate: 300 per 600 s (bucket `point`). Cache: 5 s edge.",
    operation_id="point",
    responses=spec.ok(spec.EX_POINT, spec.R429, spec.R422, spec.R503,
                      schema=spec.SCH_POINT),
    openapi_extra=spec.STABLE,
)
def point(lat: spec.Lat, lon: spec.Lon, radius: spec.RadiusNM,
          request: Request, response: Response):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.point_rate_limit,
                       settings.rate_window_s, bucket="point")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ApiError(422, "invalid_request", "invalid coordinates")
    if radius <= 0:
        raise ApiError(422, "invalid_request", "invalid radius")
    radius = min(radius, float(settings.max_point_radius_nm))
    started = time.time()
    snapshot = _fresh_snapshot(request)
    hits = []
    for entry in snapshot.aircraft:
        if entry.get("lat") is None or entry.get("lon") is None:
            continue
        dst = _distance_nm(lat, lon, entry["lat"], entry["lon"])
        if dst <= radius:
            hits.append(dict(entry, dst=round(dst, 1)))
    hits.sort(key=lambda e: e["dst"])
    response.headers["Cache-Control"] = CACHE_POINT
    return {
        "ac": hits,
        "msg": "No error",
        "now": snapshot.generated_at,
        "total": len(hits),
        "ctime": snapshot.generated_at,
        "ptime": round((time.time() - started) * 1000, 3),
    }
