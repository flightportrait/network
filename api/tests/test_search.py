"""/v1/search: one box over airframes, flights, airports, airlines."""
from app.legs_db import LegBook

from test_legs import LEGS, _build
from test_refdata import _seed_all


def _ready(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    db = tmp_path / "legs.db"
    _build(str(db), LEGS)
    app.state.legs = LegBook(str(db))
    return client, settings


def test_registration_prefix_and_bare_form(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    body = client.get("/v1/search", params={"q": "9v-sh"}).json()
    assert body["q"] == "9V-SH"
    regs = [r["label"] for r in body["results"] if r["kind"] == "aircraft"]
    assert regs == ["9V-SHA", "9V-SHB"]
    assert body["results"][0]["id"] == "76cd01"
    assert "Airbus A350-900" in body["results"][0]["detail"]
    # typed without the dash, still found
    bare = client.get("/v1/search", params={"q": "9VSHA"}).json()
    assert [r["label"] for r in bare["results"]] == ["9V-SHA"]
    # letters alone never match the dashless form: a city is not a tail
    words = client.get("/v1/search", params={"q": "9VSH"}).json()
    assert [r["label"] for r in words["results"]] == ["9V-SHA", "9V-SHB"]
    none = client.get("/v1/search", params={"q": "VSHA"}).json()
    assert none["results"] == []


def test_hex_prefix(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    body = client.get("/v1/search", params={"q": "76CD"}).json()
    assert {r["id"] for r in body["results"] if r["kind"] == "aircraft"} \
        == {"76cd01", "76cd02"}


def test_flight_numbers_icao_and_iata(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    body = client.get("/v1/search", params={"q": "SQ3"}).json()
    flights = [r for r in body["results"] if r["kind"] == "flight"]
    assert [f["id"] for f in flights] == ["SQ317", "SQ322"]
    assert flights[0]["detail"] == "1 flights, last 2026-08-26"
    # a flight page link is the callsign itself
    assert client.get("/v1/flights/" + flights[0]["id"]).status_code == 200


def test_airports_by_code_name_and_city(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    for q in ("SIN", "WSSS", "singapore", "Changi"):
        body = client.get("/v1/search", params={"q": q}).json()
        hits = [r for r in body["results"] if r["kind"] == "airport"]
        assert hits and hits[0]["id"] == "SIN", q
        assert hits[0]["label"] == "Singapore Changi (SIN)"
        assert hits[0]["detail"] == "Singapore · SG"


def test_airlines_by_code_and_name(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    for q in ("SIA", "SQ", "singapore air"):
        body = client.get("/v1/search", params={"q": q}).json()
        hits = [r for r in body["results"] if r["kind"] == "airline"]
        assert hits and hits[0]["id"] == "SIA", q
        assert hits[0]["detail"] == "SIA · SQ"


def test_short_and_empty_queries(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    assert client.get("/v1/search", params={"q": "s"}).status_code == 422
    assert client.get("/v1/search").status_code == 422
    body = client.get("/v1/search", params={"q": "zzzz"}).json()
    assert body["results"] == []


def test_search_without_legs_artifact_still_answers(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    app.state.legs = LegBook(str(tmp_path / "missing.db"))
    body = client.get("/v1/search", params={"q": "SQ322"}).json()
    assert [r["kind"] for r in body["results"]] == []
    assert client.get("/v1/search", params={"q": "9V-SHA"}).json()["results"]


def test_search_bucket_and_cache(ctx, tmp_path):
    client, settings = _ready(ctx, tmp_path)
    resp = client.get("/v1/search", params={"q": "SIN"})
    assert resp.headers["Cache-Control"] == "public, s-maxage=600"
    settings.search_rate_limit = 1
    assert client.get("/v1/search", params={"q": "SIN"}).status_code == 429
    assert client.get("/v1/now").status_code == 200
