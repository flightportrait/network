"""The station poller: registration, sessions, coarse coords, fallback."""
import datetime

import httpx
import pytest

from app import stations as stations_logic
from app.models import Station, StationSession
from app.poller import poll_receivers_once, poll_snapshot_once, \
    poll_stations_once
from app.stations import utcnow

UUID_A = "de1e7e00-0000-4000-8000-000000000001"
UUID_B = "11112222-3333-4444-5555-666677778888"
ANON = "c54a4160-589d-f496-0000-000000000000"


def row(uuid, conn_time=100, msgs=1.0, positions=50):
    return [uuid, "10.0.0.1 port 1", 0.1, conn_time, msgs, 0.5, 0, 20,
            positions]


@pytest.mark.anyio
async def anyio_unused():
    pass


def _poll(app, clients):
    app.state.readsb.clients_payload = {"now": 0, "clients": clients}
    import asyncio
    asyncio.new_event_loop().run_until_complete(poll_stations_once(app))


def test_poller_registers_and_ignores_anonymous(ctx):
    client, app, sm, settings, readsb = ctx
    _poll(app, [row(UUID_A), row(ANON)])
    session = sm()
    stations = session.query(Station).all()
    assert len(stations) == 1
    assert stations[0].half_id == UUID_A.replace("-", "")[:16]
    session.close()
    assert app.state.presence_available is True
    assert len(app.state.presence) == 1


def test_poller_first_seen_stable_last_seen_advances(ctx):
    client, app, sm, settings, readsb = ctx
    _poll(app, [row(UUID_A)])
    session = sm()
    first = session.query(Station).one().first_seen
    session.close()
    _poll(app, [row(UUID_A, conn_time=200)])
    session = sm()
    station = session.query(Station).one()
    assert station.first_seen == first
    assert station.last_seen >= first
    session.close()


def test_disappearance_closes_session_reconnect_opens_new(ctx):
    client, app, sm, settings, readsb = ctx
    _poll(app, [row(UUID_A, conn_time=500)])
    _poll(app, [])                       # station vanished
    session = sm()
    closed = session.query(StationSession).one()
    assert closed.ended_at is not None
    session.close()
    _poll(app, [row(UUID_A, conn_time=5)])   # fresh connection
    session = sm()
    sessions = session.query(StationSession).order_by(
        StationSession.started_at).all()
    assert len(sessions) == 2
    assert sessions[-1].ended_at is None
    session.close()


def test_receivers_sets_coarse_coords_ignores_bad_extent(ctx):
    client, app, sm, settings, readsb = ctx
    _poll(app, [row(UUID_A), row(UUID_B)])
    half_a = UUID_A.replace("-", "")[:16]
    half_b = UUID_B.replace("-", "")[:16]
    readsb.receivers_payload = {"receivers": [
        [half_a, 1.0, 0, 1.2, 1.5, 103.6, 104.0, 0, 1.34567, 103.81234],
        [half_b, 1.0, 0, 0, 0, 0, 0, 1, 50.0, 8.0],     # badExtent
    ]}
    import asyncio
    asyncio.new_event_loop().run_until_complete(poll_receivers_once(app))
    session = sm()
    by_half = {s.half_id: s for s in session.query(Station).all()}
    assert by_half[half_a].coarse_lat == 1.3       # rounded to 0.1 deg
    assert by_half[half_a].coarse_lon == 103.8
    assert by_half[half_b].coarse_lat is None
    session.close()


def test_prune_sessions(ctx):
    client, app, sm, settings, readsb = ctx
    _poll(app, [row(UUID_A)])
    _poll(app, [])
    session = sm()
    old = session.query(StationSession).one()
    old.ended_at = utcnow() - datetime.timedelta(days=400)
    session.commit()
    removed = stations_logic.prune_sessions(session,
                                            settings.session_retention_days)
    assert removed == 1
    session.close()


def test_clients_404_enters_fallback_mode(ctx):
    client, app, sm, settings, readsb = ctx
    request = httpx.Request("GET", "http://aggregator/data/clients.json")
    readsb.clients_payload = httpx.HTTPStatusError(
        "404", request=request,
        response=httpx.Response(404, request=request))
    import asyncio
    asyncio.new_event_loop().run_until_complete(poll_stations_once(app))
    assert app.state.presence_available is False


def test_snapshot_poll_updates_state(ctx):
    client, app, sm, settings, readsb = ctx
    readsb.aircraft_payload = {"now": 1234.0,
                               "aircraft": [{"hex": "abc123", "lat": 1.0,
                                             "lon": 2.0}]}
    import asyncio
    asyncio.new_event_loop().run_until_complete(poll_snapshot_once(app))
    assert app.state.snapshot.aircraft_count == 1
    assert app.state.snapshot.with_pos_count == 1
    assert app.state.snapshot.generated_at == 1234.0


def test_point_source_mode(ctx):
    client, app, sm, settings, readsb = ctx
    settings.source_mode = "point"
    settings.source_lat, settings.source_lon = 48.8, 2.3
    readsb.point_payload = {"now": 4321.0,
                            "aircraft": [{"hex": "abc123", "lat": 48.9,
                                          "lon": 2.4}]}
    import asyncio
    asyncio.new_event_loop().run_until_complete(poll_snapshot_once(app))
    assert app.state.snapshot.aircraft_count == 1
    assert app.state.snapshot.generated_at == 4321.0
    call = [c for c in readsb.calls
            if isinstance(c, tuple) and c[0] == "point_source"][0]
    assert call[2] == 48.8 and call[3] == 2.3
