"""/v2/point: ecosystem envelope over the snapshot — filtering, clamping,
sorting, and the no-aggregator-load invariant."""
import time

from app.snapshot import build_snapshot

SKY = [
    {"hex": "76cd06", "flight": "SIA123", "t": "A359",
     "lat": 1.36, "lon": 103.82, "alt_baro": 6325, "gs": 285.9},
    {"hex": "76cd07", "lat": 2.35, "lon": 103.82},      # ~60 nm north
    {"hex": "76cd08"},                                  # no position
]


def _seed(app, settings):
    app.state.snapshot = build_snapshot(
        {"now": time.time(), "aircraft": SKY}, settings.max_aircraft)


def test_point_filters_and_sorts(ctx):
    client, app, sm, settings, readsb = ctx
    _seed(app, settings)
    body = client.get("/v2/point/1.35/103.82/50").json()
    assert body["total"] == 1
    assert body["msg"] == "No error"
    assert body["ac"][0]["hex"] == "76cd06"
    assert 0 < body["ac"][0]["dst"] < 2          # nm to the point
    wide = client.get("/v2/point/1.35/103.82/250").json()
    assert [a["hex"] for a in wide["ac"]] == ["76cd06", "76cd07"]
    assert wide["ac"][0]["dst"] < wide["ac"][1]["dst"]   # nearest first


def test_point_radius_clamped(ctx):
    client, app, sm, settings, readsb = ctx
    _seed(app, settings)
    # 76cd07 is ~60 nm out; a 9999 request clamps to 250 and still sees it
    body = client.get("/v2/point/1.35/103.82/9999").json()
    assert body["total"] == 2


def test_point_never_touches_the_aggregator(ctx):
    client, app, sm, settings, readsb = ctx
    _seed(app, settings)
    client.get("/v2/point/1.35/103.82/250")
    assert readsb.calls == []      # snapshot only, by design


def test_point_invalid_input(ctx):
    client, app, sm, settings, readsb = ctx
    _seed(app, settings)
    for path in ("/v2/point/91/0/50", "/v2/point/0/181/50",
                 "/v2/point/0/0/0", "/v2/point/abc/0/50"):
        response = client.get(path)
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_request"


def test_point_throttled(ctx):
    client, app, sm, settings, readsb = ctx
    from app import ratelimit
    _seed(app, settings)
    settings.point_rate_limit = 2
    ratelimit.reset()
    assert client.get("/v2/point/1.35/103.82/50").status_code == 200
    assert client.get("/v2/point/1.35/103.82/50").status_code == 200
    response = client.get("/v2/point/1.35/103.82/50")
    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"
    assert int(response.headers["retry-after"]) >= 1
    assert response.headers["ratelimit-remaining"] == "0"
