"""Roster privacy and the uniform-404 self-lookup."""
from app import stations as stations_logic

# Synthetic. Knowing a station UUID is the self-view credential, so a
# fixture must never be a real feeder's — CI (.github/check.py) rejects
# any UUID whose public_id could match a live station.
RECEIVER_UUID = "de1e7e00-0000-4000-8000-000000000001"
CLIENT_ROW = [RECEIVER_UUID, "1.2.3.4 port 5", 0.09, 1877, 0.426, 0.109,
              0, 16, 204]


def _register(sm, app):
    session = sm()
    presence = stations_logic.upsert_presence(
        session, stations_logic.parse_clients({"clients": [CLIENT_ROW]}))
    session.close()
    app.state.presence = presence
    return presence


def test_roster_is_coarse_and_leak_free(ctx):
    client, app, sm, settings, readsb = ctx
    _register(sm, app)
    body = client.get("/v1/stations").json()
    assert len(body["stations"]) == 1
    entry = body["stations"][0]
    assert entry["id"].startswith("fp-")
    assert entry["online"] is True
    # Nothing that identifies or locates the feeder precisely:
    dumped = str(body)
    assert "de1e7e00" not in dumped          # no uuid material
    assert "1.2.3.4" not in dumped           # no IP, ever
    assert set(entry) == {"id", "label", "coarse_lat", "coarse_lon",
                          "first_seen", "last_seen", "online"}


def test_self_lookup_with_uuid(ctx):
    client, app, sm, settings, readsb = ctx
    _register(sm, app)
    readsb.count_payload = 3
    body = client.get("/v1/stations/%s" % RECEIVER_UUID).json()
    assert body["online"] is True
    assert body["messages_per_s"] == 0.426
    assert body["aircraft_seen"] == 3
    assert body["positions_total"] == 204
    assert len(body["recent_sessions"]) == 1


def test_self_lookup_uniform_404(ctx):
    client, app, sm, settings, readsb = ctx
    _register(sm, app)
    unknown = client.get(
        "/v1/stations/00000000-0000-4000-8000-000000000000")
    malformed = client.get("/v1/stations/not-a-uuid")
    assert unknown.status_code == malformed.status_code == 404
    assert unknown.json() == malformed.json()
    assert unknown.headers["cache-control"] == "no-store"


def test_self_lookup_accepts_dashless_uuid(ctx):
    client, app, sm, settings, readsb = ctx
    _register(sm, app)
    dashless = RECEIVER_UUID.replace("-", "")
    assert client.get("/v1/stations/%s" % dashless).status_code == 200


def test_self_lookup_count_failure_degrades(ctx):
    client, app, sm, settings, readsb = ctx
    _register(sm, app)
    readsb.count_payload = RuntimeError("readsb hiccup")
    body = client.get("/v1/stations/%s" % RECEIVER_UUID).json()
    assert body["aircraft_seen"] is None      # degraded stat, not a 500
