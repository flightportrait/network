"""Live-sky routes: the snapshot views and the ecosystem-compatible
point query. Throttle first, always; cache headers explicit on every
response so Cloudflare's behavior is decided here, not by defaults.

Everything here reads the in-memory snapshot and nothing else — the load
on readsb is the poller's constant ~0.2 req/s no matter what the public
does. /v2/point included: it filters the snapshot by distance instead of
proxying the aggregator, so its envelope stays ecosystem-shaped while its
cost stays memory-only.
"""
import asyncio
import math
import time

from fastapi import APIRouter, Query, Request, Response, WebSocket, \
    WebSocketDisconnect

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
    state = request.app.state
    # station_count is null unless a station poll succeeded recently — a
    # stalled clients poller must not keep reporting vanished feeders as
    # connected (the sky snapshot and the station poll fail independently).
    fresh_presence = (time.time() - state.presence_at
                      <= settings.station_presence_stale_s)
    known = state.presence_available and fresh_presence
    response.headers["Cache-Control"] = CACHE_LIVE
    legs = getattr(state, "legs", None)
    return {
        "aircraft_count": snapshot.aircraft_count,
        "aircraft_with_pos": snapshot.with_pos_count,
        "station_count": len(state.presence) if known else None,
        "generated_at": snapshot.generated_at,
        # the history's edge: the newest day in the legs artifact, null
        # while the archive is dark
        "archive_through": legs.archive_through() if legs else None,
    }


def _parse_bbox(bbox: str | None):
    """`minLon,minLat,maxLon,maxLat` -> tuple, or None. A box whose west
    edge is east of its east edge crosses the antimeridian."""
    if not bbox:
        return None
    try:
        parts = [float(x) for x in bbox.split(",")]
    except ValueError:
        parts = []
    if len(parts) != 4 or not all(math.isfinite(x) for x in parts):
        raise ApiError(422, "invalid_request",
                       "bbox is minLon,minLat,maxLon,maxLat")
    w, s, e, n = parts
    if not (-90 <= s <= n <= 90) or not (-180 <= w <= 180 and -180 <= e <= 180):
        raise ApiError(422, "invalid_request", "bbox out of range")
    return w, s, e, n


def _in_bbox(item: dict, box) -> bool:
    lat, lon = item.get("lat"), item.get("lon")
    if lat is None or lon is None:
        return False
    w, s, e, n = box
    if not (s <= lat <= n):
        return False
    if w <= e:
        return w <= lon <= e
    return lon >= w or lon <= e          # crosses the antimeridian


@router.get(
    "/v1/aircraft", tags=["Live"], summary="Aircraft",
    description="Aircraft the network hears right now, readsb field "
                "dialect. With `bbox=minLon,minLat,maxLon,maxLat` only "
                "the aircraft with a position inside it (a box whose "
                "west edge is east of its east edge crosses the "
                "antimeridian); `total` stays the whole network. 503 if "
                "the snapshot is older than 60 seconds. Rate: 300 per "
                "600 s (bucket `aircraft`). Cache: 10 s edge, 5 s browser.",
    operation_id="aircraft",
    responses=spec.ok(spec.EX_AIRCRAFT, spec.R422, spec.R429, spec.R503,
                      schema=spec.SCH_AIRCRAFT),
    openapi_extra=spec.STABLE,
)
def aircraft(request: Request, response: Response,
             bbox: str | None = Query(
                 None, max_length=80,
                 description="minLon,minLat,maxLon,maxLat; only aircraft "
                             "with a position inside.")):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.aircraft_rate_limit,
                       settings.rate_window_s, bucket="aircraft")
    box = _parse_bbox(bbox)
    snapshot = _fresh_snapshot(request)
    response.headers["Cache-Control"] = CACHE_LIVE
    listed = snapshot.aircraft
    if box is not None:
        listed = [a for a in listed if _in_bbox(a, box)]
    return {"generated_at": snapshot.generated_at,
            "total": snapshot.aircraft_count,
            "with_position": snapshot.with_pos_count,
            "aircraft": listed}


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


# ---- the live stream ------------------------------------------------------
# One socket per open map. The client says which box it looks at (or
# null for the whole sky); the server answers with everything in it,
# then, on every new snapshot, only what changed: aircraft whose fields
# moved (`upd`) and aircraft that left the box or the sky (`del`).
# `seen`/`seen_pos` ticking alone is not a change; the client ages a
# position from the moment it received it. Compression is the socket's
# own (permessage-deflate).

_open_sockets: dict[str, int] = {}
STREAM_TICK_S = 1.0
_SKIP = ("seen", "seen_pos")


def _same(a: dict, b: dict) -> bool:
    for k in set(a) | set(b):
        if k in _SKIP:
            continue
        if a.get(k) != b.get(k):
            return False
    return True


def _box_of(msg) -> tuple | None:
    box = msg.get("bbox") if isinstance(msg, dict) else None
    if box is None:
        return None
    if not (isinstance(box, list) and len(box) == 4):
        raise ValueError("bbox")
    return _parse_bbox(",".join(str(float(x)) for x in box))


@router.websocket("/v1/stream")
async def stream(ws: WebSocket):
    settings = ws.app.state.settings
    ip = ratelimit.client_ip(ws)
    if _open_sockets.get(ip, 0) >= settings.stream_max_per_ip:
        await ws.close(code=1008)
        return
    _open_sockets[ip] = _open_sockets.get(ip, 0) + 1
    await ws.accept()
    box = None
    sent: dict[str, dict] = {}
    last_gen = -1.0
    wait = 0.5          # the first box usually arrives right away
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=wait)
            except asyncio.TimeoutError:
                msg = None
            except (ValueError, TypeError):
                await ws.close(code=1003)
                return
            wait = STREAM_TICK_S
            full = False
            if msg is not None:
                try:
                    new_box = _box_of(msg)
                except (ValueError, ApiError):
                    await ws.close(code=1003)
                    return
                if new_box != box or not sent and last_gen < 0:
                    box, full = new_box, True
            snap = ws.app.state.snapshot
            if not snap.fresh(settings.stale_after_s):
                if last_gen != 0:
                    await ws.send_json({"stale": True})
                    last_gen = 0
                continue
            if snap.generated_at == last_gen and not full:
                continue
            if last_gen < 0:
                full = True          # the first word is always everything
            last_gen = snap.generated_at
            listed = snap.aircraft
            if box is not None:
                listed = [a for a in listed if _in_bbox(a, box)]
            now_keys = set()
            upd = []
            for a in listed:
                h = a.get("hex")
                if not h:
                    continue
                now_keys.add(h)
                prev = sent.get(h)
                if full or prev is None or not _same(prev, a):
                    upd.append(a)
                    sent[h] = a
            gone = [h for h in sent if h not in now_keys]
            for h in gone:
                del sent[h]
            if full or upd or gone:
                out = {"t": snap.generated_at, "total": snap.aircraft_count,
                       "with_position": snap.with_pos_count, "upd": upd,
                       "del": gone}
                if full:
                    out["full"] = True
                await ws.send_json(out)
            else:
                # nothing moved: still say the sky was heard
                await ws.send_json({"t": snap.generated_at})
    except WebSocketDisconnect:
        pass
    finally:
        _open_sockets[ip] = _open_sockets.get(ip, 1) - 1
        if _open_sockets[ip] <= 0:
            _open_sockets.pop(ip, None)
