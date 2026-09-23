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

from sqlalchemy import select

from . import openapi as spec
from . import ratelimit
from .refdata_models import RefAirport
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


_ROUTES = {}
ROUTE_TTL_S = 3600


def route_of(app, callsign):
    """A callsign's route as /v1/routes resolves it: the observed routes
    artifact first, the community catalog where it is silent. Cached an
    hour per callsign, so a lost aircraft costs one lookup."""
    now = time.time()
    hit = _ROUTES.get(callsign)
    if hit and now - hit[0] < ROUTE_TTL_S:
        return hit[1]
    route = app.state.routes.get(callsign)
    if route is None:
        from .contributions import catalog_current, catalog_route
        try:
            with app.state.sessionmaker() as session:
                current = catalog_current(session, callsign)
                route = catalog_route(current) if current is not None else None
        except Exception:                 # noqa: BLE001 — no route, no guess
            route = None
    if len(_ROUTES) > 20000:
        _ROUTES.clear()
    _ROUTES[callsign] = (now, route)
    return route


_AIRPORTS = {"at": 0.0, "coords": {}}
AIRPORTS_REFRESH_S = 6 * 3600


def airport_coords(app):
    """{IATA: (lat, lon)} from the airport reference table, cached; the
    route chains name airports by IATA."""
    now = time.time()
    if now - _AIRPORTS["at"] > AIRPORTS_REFRESH_S:
        _AIRPORTS["at"] = now
        try:
            with app.state.sessionmaker() as session:
                _AIRPORTS["coords"] = {
                    iata: (lat, lon) for iata, lat, lon in session.execute(
                        select(RefAirport.iata, RefAirport.lat, RefAirport.lon)
                        .where(RefAirport.iata.is_not(None),
                               RefAirport.lat.is_not(None),
                               RefAirport.lon.is_not(None)))}
        except Exception:                 # noqa: BLE001 — keep the last good
            pass
    return _AIRPORTS["coords"]


@router.get(
    "/v1/estimated", tags=["Live"], summary="Estimated positions",
    description="Aircraft the network stopped hearing while cruising, drawn "
                "where they most likely are: flown on from the last observed "
                "position toward the destination their callsign's route names, "
                "at the last observed speed (holding the last track 10 "
                "minutes, then turning toward the destination). Every entry is "
                "an estimate, never an observation: `estimated` is always true, "
                "`last_seen` is the last real position, `alt_baro` and `gs` are "
                "the last observed values. An aircraft appears 90 s after it "
                "was last heard and leaves when it is heard again, nears its "
                "destination, or its flight time runs out. Kept in memory, "
                "never archived or exported. Scored every night against the "
                "previous day's real coverage gaps; `accuracy` is the latest "
                "night's result for the method in use (median error by gap "
                "length), null before the first. Rate: 300 per 600 s (bucket "
                "`estimated`). Cache: 15 s edge, 10 s browser.",
    operation_id="estimated",
    responses=spec.ok(spec.EX_ESTIMATED, spec.R429, schema=spec.SCH_ESTIMATED),
    openapi_extra=spec.MAP_TIER,
)
def estimated(request: Request, response: Response):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.aircraft_rate_limit,
                       settings.rate_window_s, bucket="estimated")
    book = request.app.state.estimates
    now = time.time()
    listed = book.estimates(now) if book is not None else []
    response.headers["Cache-Control"] = "public, max-age=10, s-maxage=15"
    return {"generated_at": now, "method": "converge-to-destination",
            "accuracy": _latest_accuracy(request.app), "aircraft": listed}


_ACCURACY = {"at": 0.0, "value": None}


def _latest_accuracy(app):
    """The last night's measured accuracy of the method in use, cached
    ten minutes: {day, n, median_km, by_gap_min}, or null before the
    first night."""
    now = time.time()
    if now - _ACCURACY["at"] > 600:
        _ACCURACY["at"] = now
        try:
            from .refdata_models import EstimateScore
            with app.state.sessionmaker() as session:
                row = session.execute(select(EstimateScore)
                                      .order_by(EstimateScore.day.desc())
                                      .limit(1)).scalar_one_or_none()
            if row is not None:
                m = (row.detail.get("methods") or {}).get("converge") or {}
                _ACCURACY["value"] = {
                    "day": row.day.isoformat(), "n": m.get("n", 0),
                    "median_km": m.get("median_km"),
                    "by_gap_min": m.get("by_gap_min", {})}
        except Exception:                 # noqa: BLE001 — accuracy is optional
            pass
    return _ACCURACY["value"]


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
async def trace(hex: spec.Hex, request: Request, response: Response):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.trace_rate_limit,
                       settings.rate_window_s, bucket="trace")
    points = request.app.state.traces.get(hex)
    if not points:
        raise ApiError(404, "not_found", "no trace")
    response.headers["Cache-Control"] = CACHE_POINT
    bounds = await _flight_bounds(request.app, hex.strip().lower())
    # points: [t, lat, lon, alt_baro, track], oldest first
    return {"hex": hex.strip().lower(), "points": points,
            "departure": bounds.get("departure"),
            "arrival": bounds.get("arrival")}


BOUNDS_TTL_S = 60.0


async def _flight_bounds(app, hex_id: str) -> dict:
    """Departure and arrival from the hub's day trace, held a minute per
    aircraft so a busy card does not fetch the trace on every poll."""
    from .departure import flight_bounds
    cache = getattr(app.state, "flight_bounds", None)
    if cache is None:
        cache = app.state.flight_bounds = {}
    now = time.time()
    hit = cache.get(hex_id)
    if hit and now - hit[0] < BOUNDS_TTL_S:
        return hit[1]
    result = {}
    try:
        result = flight_bounds(await app.state.readsb.trace(hex_id)) or {}
    except Exception:            # no trace, upstream down: nothing to say
        result = {}
    if len(cache) > 5000:
        cache.clear()
    cache[hex_id] = (now, result)
    return result


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
    live = getattr(ws.app.state, "live", None)
    seen_pub = live.published if live is not None else 0
    recv_task = None
    try:
        while True:
            # wake on: a message from the client, a published change of
            # the sky, or the tick
            if recv_task is None:
                recv_task = asyncio.ensure_future(ws.receive_json())
            waiters = {recv_task}
            change = None
            if live is not None:
                change = asyncio.ensure_future(live.wait_change(seen_pub))
                waiters.add(change)
            done, _ = await asyncio.wait(waiters, timeout=wait,
                                         return_when=asyncio.FIRST_COMPLETED)
            if change is not None:
                if change in done:
                    seen_pub = change.result()
                else:
                    change.cancel()
            msg = None
            if recv_task in done:
                try:
                    msg = recv_task.result()
                except WebSocketDisconnect:
                    raise
                except (ValueError, TypeError):
                    await ws.close(code=1003)
                    return
                recv_task = None
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
        if recv_task is not None and not recv_task.done():
            recv_task.cancel()
        _open_sockets[ip] = _open_sockets.get(ip, 1) - 1
        if _open_sockets[ip] <= 0:
            _open_sockets.pop(ip, None)
