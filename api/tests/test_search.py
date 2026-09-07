"""/v1/search: one box over airframes, flights, airports, airlines."""
from app.legs_db import LegBook
from app.refdata_models import RefAirline, RefAirport, RefSchedule

from test_legs import LEGS, _build
from test_refdata import _seed_all


SCHEDULE = [
    ("SIA322", "SIN", "LHR", 46), ("SIA317", "LHR", "SIN", 40),
    ("SIA21", "SIN", "EWR", 30), ("SIA22", "EWR", "SIN", 30),
    ("SIA211", "SIN", "SYD", 12),
]


def _ready(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    for cs, org, dst, n in SCHEDULE:
        session.add(RefSchedule(callsign=cs, org=org, dst=dst,
                                airline_icao="SIA", n_flights=n))
    session.add(RefAirport(ident="EGLL", name="London Heathrow Airport",
                           kind="large_airport", iso_country="GB",
                           municipality="London", iata="LHR"))
    session.commit(); session.close()
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
    # the schedule answers first, busiest number first, with its route;
    # the legs artifact adds the one-offs it knows (SQ-prefixed rows)
    assert [f["id"] for f in flights] == ["SIA322", "SIA317", "SQ317", "SQ322"]
    assert flights[0]["detail"] == "SIN \u2192 LHR · 46 flights"
    assert flights[2]["detail"] == "1 flights, last 2026-08-26"
    # a bare airline prefix never touches the legs artifact
    body = client.get("/v1/search", params={"q": "SIA"}).json()
    flights = [r["id"] for r in body["results"] if r["kind"] == "flight"]
    assert flights == ["SIA322", "SIA317", "SIA21", "SIA22", "SIA211"]
    assert client.get("/v1/flights/SQ317").status_code == 200


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
    session = sm()
    session.add(RefSchedule(callsign="SIA322", org="SIN", dst="LHR",
                            airline_icao="SIA", n_flights=46))
    session.commit(); session.close()
    app.state.legs = LegBook(str(tmp_path / "missing.db"))
    # the schedule still answers; only the one-off fallback is dark
    body = client.get("/v1/search", params={"q": "SQ322"}).json()
    assert [r["id"] for r in body["results"]] == ["SIA322"]
    assert client.get("/v1/search", params={"q": "9V-SHA"}).json()["results"]


def test_search_bucket_and_cache(ctx, tmp_path):
    client, settings = _ready(ctx, tmp_path)
    resp = client.get("/v1/search", params={"q": "SIN"})
    assert resp.headers["Cache-Control"] == "public, s-maxage=43200"
    settings.search_rate_limit = 1
    assert client.get("/v1/search", params={"q": "SIN"}).status_code == 429
    assert client.get("/v1/now").status_code == 200


def test_words_put_places_first_and_skip_the_tail_scan(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    body = client.get("/v1/search", params={"q": "Singapore"}).json()
    kinds = [r["kind"] for r in body["results"]]
    assert kinds[0] == "airport" and "airline" in kinds
    assert "aircraft" not in kinds and "flight" not in kinds
    # a code-shaped query answers aircraft and flights first
    body = client.get("/v1/search", params={"q": "SQ32"}).json()
    assert body["results"][0]["kind"] == "flight"
    # one ranked list: an exact code outranks every prefix
    body = client.get("/v1/search", params={"q": "SIN"}).json()
    assert body["results"][0]["id"] == "SIN"
    assert body["results"][0]["score"] > body["results"][-1]["score"]


def test_rows_without_a_code_never_outrank_the_real_one(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    session.add(RefAirport(ident="WSAC", name="Changi Air Base (East)",
                           kind="medium_airport", iso_country="SG",
                           municipality="Singapore", iata=None))
    session.add(RefAirline(icao="SQC", iata=None,
                           name="Singapore Airlines Cargo"))
    session.commit(); session.close()
    body = client.get("/v1/search", params={"q": "Singapore"}).json()
    airports = [r["id"] for r in body["results"] if r["kind"] == "airport"]
    airlines = [r["id"] for r in body["results"] if r["kind"] == "airline"]
    assert airports[0] == "SIN" and airlines[0] == "SIA"


def test_route_between_two_places(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    for q in ("SIN LHR", "Singapore London", "WSSS LHR"):
        body = client.get("/v1/search", params={"q": q}).json()
        hits = [r for r in body["results"] if r["kind"] == "flight"]
        assert hits and hits[0]["id"] == "SIA322", q
        assert hits[0]["detail"] == "SIN \u2192 LHR · 46 flights"
    # the other way round is the other flight number
    body = client.get("/v1/search", params={"q": "LHR SIN"}).json()
    assert body["results"][0]["id"] == "SIA317"


def test_fleet_by_operator_and_type(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    for q in ("Singapore A350", "SQ A359", "A350 SIA"):
        body = client.get("/v1/search", params={"q": q}).json()
        regs = [r["label"] for r in body["results"] if r["kind"] == "aircraft"]
        assert regs == ["9V-SHA", "9V-SHB"], q


def test_busiest_airport_wins_the_city(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    session.add(RefAirport(ident="WSAC", name="Changi Air Base (East)",
                           kind="large_airport", iso_country="SG",
                           municipality="Singapore", iata="QPG"))
    for cs, org, dst, n in SCHEDULE:
        session.add(RefSchedule(callsign=cs, org=org, dst=dst,
                                airline_icao="SIA", n_flights=n))
    session.commit(); session.close()
    body = client.get("/v1/search", params={"q": "Singapore"}).json()
    airports = [r["id"] for r in body["results"] if r["kind"] == "airport"]
    assert airports[0] == "SIN"
