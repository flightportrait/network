"""The routes artifact: loading, lookup via /v1/flights, absence, reload."""
import gzip
import json

from app.legs_db import LegBook
from app.routes_db import RouteBook


def _write(path, data):
    with gzip.open(path, "wt") as fh:
        json.dump(data, fh)


def test_route_lookup(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    artifact = tmp_path / "routes.json.gz"
    _write(artifact, {"SIA321": ["SIN", "LHR"], "qfa5": ["SYD", "PER", "FCO"]})
    app.state.routes = RouteBook(str(artifact))
    app.state.legs = LegBook(str(tmp_path / "absent.db"))
    body = client.get("/v1/flights/SIA321").json()
    assert body["callsign"] == "SIA321"
    assert body["route"] == ["SIN", "LHR"]
    assert body["legs"] is None          # log artifact dark: null, not []
    # normalization both ways, and multi-leg chains pass through
    assert client.get("/v1/flights/qfa5").json()["route"] == \
        ["SYD", "PER", "FCO"]


def test_route_unknown_404_no_store(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    artifact = tmp_path / "routes.json.gz"
    _write(artifact, {"SIA321": ["SIN", "LHR"]})
    app.state.routes = RouteBook(str(artifact))
    app.state.legs = LegBook(str(tmp_path / "absent.db"))
    response = client.get("/v1/flights/NOPE999")
    assert response.status_code == 404
    assert response.json()["error"] == "not_observed"
    assert response.headers["cache-control"] == "no-store"


def test_missing_artifact_is_dark_not_broken(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    app.state.routes = RouteBook(str(tmp_path / "absent.json.gz"))
    app.state.legs = LegBook(str(tmp_path / "absent.db"))
    response = client.get("/v1/flights/SIA321")
    assert response.status_code == 503
    assert response.json()["error"] == "artifact_unavailable"
    assert app.state.routes.count() == 0
    assert app.state.routes.available() is False


def test_reload_on_mtime_change(tmp_path):
    artifact = tmp_path / "routes.json.gz"
    _write(artifact, {"AAA111": ["AAA", "BBB"]})
    book = RouteBook(str(artifact), reload_s=0.0)
    assert book.get("AAA111") == ["AAA", "BBB"]
    _write(artifact, {"CCC222": ["CCC", "DDD"]})
    import os
    os.utime(artifact, (0, 9999999999))
    assert book.get("CCC222") == ["CCC", "DDD"]
    assert book.get("AAA111") is None
