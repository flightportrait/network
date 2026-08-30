"""/healthz, /, /v1/now, /v1/aircraft: freshness, allowlist, caching,
the error envelope, and the OpenAPI contract lock."""
import json
import time
from pathlib import Path

from app.snapshot import AIRCRAFT_FIELDS, Snapshot, build_snapshot

A350 = {
    "hex": "76cd06", "flight": "SIA123 ", "t": "A359", "r": "9V-SHF",
    "lat": 1.5, "lon": 103.8, "alt_baro": 6325, "gs": 285.9,
    "track": 85.4, "category": "A5", "squawk": "2136",
    "seen": 0.2, "seen_pos": 1.1,
    # fields that must NOT pass through:
    "rssi": -21.5, "nav_qnh": 1011.2, "mach": 0.424,
}


def test_healthz_and_index(ctx):
    client, app, sm, settings, readsb = ctx
    assert client.get("/healthz").json() == {"ok": True}
    index = client.get("/").json()
    assert index["docs"].startswith("https://docs.flightportrait.com")
    assert index["openapi"] == "/openapi.json"
    assert index["swagger"] == "/docs"
    assert index["source"] == "https://github.com/flightportrait/network"
    assert "feed.flightportrait.com" in index["feed"]
    assert "ODbL" in index["attribution"]


def test_now_counts_and_cache(ctx):
    client, app, sm, settings, readsb = ctx
    app.state.snapshot = build_snapshot(
        {"now": time.time(), "aircraft": [A350, {"hex": "abc123"}]},
        settings.max_aircraft)
    app.state.presence = {"deadbeefdeadbeef": {}}
    response = client.get("/v1/now")
    assert response.status_code == 200
    body = response.json()
    assert body["aircraft_count"] == 2
    assert body["aircraft_with_pos"] == 1
    assert body["station_count"] == 1
    assert body["generated_at"] > 0
    assert "s-maxage=10" in response.headers["cache-control"]


def test_now_station_count_null_in_fallback_mode(ctx):
    client, app, sm, settings, readsb = ctx
    app.state.presence_available = False
    assert client.get("/v1/now").json()["station_count"] is None


def test_stale_snapshot_is_503_no_store(ctx):
    client, app, sm, settings, readsb = ctx
    app.state.snapshot = Snapshot(generated_at=time.time() - 3600)
    for path in ("/v1/now", "/v1/aircraft", "/v2/point/1.35/103.82/50"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["retry-after"] == "5"
        body = response.json()
        assert body["error"] == "stale_snapshot"
        assert body["detail"]


def test_error_envelope_uniform(ctx):
    client, app, sm, settings, readsb = ctx
    # hand-raised 404, framework 404 (unknown path), framework 422
    # (path-type mismatch): one envelope, always no-store
    for path, code in (("/v1/trace/aaaaaa", "not_found"),
                       ("/v1/nope", "not_found"),
                       ("/v2/point/abc/0/50", "invalid_request")):
        response = client.get(path)
        body = response.json()
        assert set(body) == {"error", "detail"}, path
        assert body["error"] == code
        assert isinstance(body["detail"], str)
        assert response.headers["cache-control"] == "no-store"


def test_aircraft_field_allowlist(ctx):
    client, app, sm, settings, readsb = ctx
    app.state.snapshot = build_snapshot(
        {"now": time.time(), "aircraft": [A350]}, settings.max_aircraft)
    body = client.get("/v1/aircraft").json()
    assert body["generated_at"] > 0
    entry = body["aircraft"][0]
    assert set(entry) <= set(AIRCRAFT_FIELDS)
    assert "rssi" not in entry and "nav_qnh" not in entry
    assert entry["flight"] == "SIA123"          # stripped


def test_aircraft_cap(ctx):
    client, app, sm, settings, readsb = ctx
    settings.max_aircraft = 3
    app.state.snapshot = build_snapshot(
        {"now": time.time(),
         "aircraft": [{"hex": "%06x" % i} for i in range(10)]},
        settings.max_aircraft)
    assert len(client.get("/v1/aircraft").json()["aircraft"]) == 3


OPENAPI_PATHS = {
    "/", "/healthz", "/v1/now", "/v1/aircraft",
    "/v1/trace/{hex}",
    "/v2/point/{lat}/{lon}/{radius}",
    "/v1/airframes/{hex}",
    "/v1/flights/{callsign}",
    "/v1/airports/{code}",
    "/v1/stations", "/v1/stations/{station_uuid}",
    "/v1/airlines",
    "/v1/airlines/{icao}",
    "/v1/airlines/{icao}/routes",
    "/v1/airlines/{icao}/leg/{org}/{dst}",
    "/v1/airlines/{icao}/schedule/{org}/{dst}",
    "/v1/airlines/{icao}/countries",
    "/v1/airlines/{icao}/fleet",
    "/v1/airlines/{icao}/fleet/{designator}",
    "/v1/alliances",
    "/v1/alliances/{slug}",
    "/v1/alliances/{slug}/routes",
    "/v1/types/{designator}",
}


def test_openapi_paths_exact(ctx):
    client, app, sm, settings, readsb = ctx
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == OPENAPI_PATHS


STABLE_PATHS = {
    "/", "/healthz", "/v1/now", "/v1/aircraft", "/v1/trace/{hex}",
    "/v2/point/{lat}/{lon}/{radius}", "/v1/airframes/{hex}",
    "/v1/flights/{callsign}", "/v1/airports/{code}",
    "/v1/stations", "/v1/stations/{station_uuid}",
    "/v1/airlines", "/v1/airlines/{icao}", "/v1/types/{designator}",
}


def test_openapi_metadata(ctx):
    client, app, sm, settings, readsb = ctx
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"] == "FlightPortrait network API"
    assert spec["servers"] == [{"url": "https://data.flightportrait.com"}]
    assert {t["name"] for t in spec["tags"]} == {
        "Live", "History", "Stations", "Reference", "Meta"}
    ids = []
    for path, item in spec["paths"].items():
        op = item["get"]
        assert op.get("summary"), path
        assert op.get("description"), path
        assert op.get("tags"), path
        ids.append(op["operationId"])
        expected = "stable" if path in STABLE_PATHS else "map"
        assert op.get("x-stability") == expected, path
        if path in ("/", "/healthz"):
            assert op.get("x-hidden") is True
        else:
            assert "429" in op["responses"], path
    assert len(ids) == len(set(ids))
    assert spec["paths"]["/v1/aircraft"]["get"]["tags"] == ["Live"]
    content = (spec["paths"]["/v1/aircraft"]["get"]["responses"]["200"]
               ["content"]["application/json"])
    assert set(content["example"]["aircraft"][0]) <= set(AIRCRAFT_FIELDS)
    # every stable operation ships a real 200 schema, not just an example
    for path in STABLE_PATHS:
        ok = spec["paths"][path]["get"]["responses"]["200"]
        assert "schema" in ok["content"]["application/json"], path


def test_docs_site_openapi_matches(ctx):
    # The published docs (github.com/flightportrait/docs) carry a spec
    # snapshot; when that checkout is present alongside, it must match.
    docs = None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs-site" / "openapi.json"
        if candidate.is_file():
            docs = candidate
            break
    if docs is None:
        import pytest
        pytest.skip("docs-site not checked out")
    client, app, sm, settings, readsb = ctx
    assert json.loads(docs.read_text()) == client.get("/openapi.json").json()
