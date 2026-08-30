"""Background pollers, owned by the app lifespan.

Two loops: the sky snapshot (fast) and the stations registry (slower).
Each iteration is independently fallible — one bad poll logs and backs
off, it never kills the task or the app. Tests drive the *_once functions
synchronously; the loops exist only in production.
"""
import asyncio
import logging
import time

import httpx

from . import stations as stations_logic
from .snapshot import build_snapshot

log = logging.getLogger("network-api.poller")


async def poll_snapshot_once(app) -> None:
    settings = app.state.settings
    if settings.source_mode == "point":
        raw = await app.state.readsb.point_source(
            settings.source_point_url, settings.source_lat,
            settings.source_lon, settings.source_radius_nm)
    else:
        raw = await app.state.readsb.aircraft()
    app.state.snapshot = build_snapshot(raw, settings.max_aircraft)
    app.state.traces.record(app.state.snapshot)


async def poll_stations_once(app) -> None:
    try:
        raw = await app.state.readsb.clients()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # Degraded mode, decided at design time: this readsb build does
            # not serve clients.json. station_count goes null, self-lookup
            # keeps working against whatever the registry already holds.
            app.state.presence_available = False
            return
        raise
    app.state.presence_available = True
    rows = stations_logic.parse_clients(raw)
    session = app.state.sessionmaker()
    try:
        app.state.presence = stations_logic.upsert_presence(session, rows)
        stations_logic.prune_sessions(
            session, app.state.settings.session_retention_days)
    finally:
        session.close()
    # Stamp a successful poll. Any later failure (not just a 404) backs
    # the loop off silently, so /v1/now uses this to null the count once
    # the presence map is stale, instead of reporting old feeders as live.
    app.state.presence_at = time.time()


async def poll_receivers_once(app) -> None:
    try:
        raw = await app.state.readsb.receivers()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return
        raise
    session = app.state.sessionmaker()
    try:
        stations_logic.apply_receivers(
            session, raw, app.state.settings.coarse_decimals)
    finally:
        session.close()


async def _loop(app, fn, interval_s: float, name: str) -> None:
    backoff = interval_s
    while True:
        try:
            await fn(app)
            backoff = interval_s
        except asyncio.CancelledError:
            raise
        except Exception as exc:      # noqa: BLE001 — the loop must survive
            log.warning("%s poll failed: %s", name, exc)
            backoff = min(backoff * 2, 300.0)
        await asyncio.sleep(backoff)


def start_pollers(app) -> list:
    settings = app.state.settings
    if settings.source_mode == "point":
        # remote-source instances have no local readsb: no stations, no
        # receivers, and a polite floor on how often we poll a public
        # aggregator that is not ours to hammer
        settings.snapshot_poll_s = max(settings.snapshot_poll_s, 60.0)
        app.state.presence_available = False
        return [
            asyncio.create_task(
                _loop(app, poll_snapshot_once, settings.snapshot_poll_s,
                      "snapshot")),
        ]
    return [
        asyncio.create_task(
            _loop(app, poll_snapshot_once, settings.snapshot_poll_s,
                  "snapshot")),
        asyncio.create_task(
            _loop(app, poll_stations_once, settings.station_poll_s,
                  "stations")),
        asyncio.create_task(
            _loop(app, poll_receivers_once, settings.receivers_poll_s,
                  "receivers")),
    ]
