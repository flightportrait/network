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


def test_the_exact_number_is_scored_before_the_cap(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    session.add(RefAirline(icao="BAW", iata="BA", name="British Airways"))
    session.add(RefAirline(icao="UAE", iata="EK", name="Emirates"))
    # five busier numbers typed as BA16… fill a prefix's five rows
    for cs, n in (("BA1611", 5), ("BA1606", 4), ("BA1608", 4),
                  ("BA1635", 4), ("BA1637", 4), ("BAW16", 62),
                  ("BAW168", 1), ("UAE110", 55), ("UAE17K", 55),
                  ("UAE19", 53), ("UAE11M", 51), ("UAE185", 51),
                  ("UAE1", 30)):
        session.add(RefSchedule(callsign=cs, org="LHR", dst="SIN",
                                airline_icao=cs[:3], n_flights=n))
    session.commit(); session.close()
    app.state.legs = LegBook(str(tmp_path / "missing.db"))
    body = client.get("/v1/search", params={"q": "BA16"}).json()
    flights = [r["id"] for r in body["results"] if r["kind"] == "flight"]
    assert flights[0] == "BAW16" and len(flights) == 5
    # the prefix's own number, though five busier ones start with it
    for q in ("UAE1", "EK1"):
        body = client.get("/v1/search", params={"q": q}).json()
        assert body["results"][0]["id"] == "UAE1", q


def test_a_flight_number_typed_with_a_space(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    for q in ("SQ 322", "sq 322", "SIA 322", "SQ322"):
        body = client.get("/v1/search", params={"q": q}).json()
        assert body["results"][0]["id"] == "SIA322", q
        assert body["q"] == q.upper()
    # a registration or a pair of places is not a flight number
    from app.routes_search import _compact_flight
    assert _compact_flight("SQ 322") == "SQ322"
    assert _compact_flight("BA 16A") == "BA16A"
    assert _compact_flight("737 800") == "737 800"
    assert _compact_flight("SIN LHR") == "SIN LHR"
    assert _compact_flight("9V SMA") == "9V SMA"


def test_routes_asked_in_words(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    for q in ("Singapore to London", "from SIN to LHR", "to LHR from SIN",
              "SIN-LHR", "SIN - LHR", "SIN–LHR", "SIN — LHR",
              "SIN → LHR", "SIN>LHR", "SIN->LHR", "Singapore - London",
              "flights from Singapore to London",
              "flights to London from Singapore", "Changi to Heathrow"):
        body = client.get("/v1/search", params={"q": q}).json()
        hits = [r for r in body["results"] if r["kind"] == "flight"]
        assert hits and hits[0]["id"] == "SIA322", q
    body = client.get("/v1/search", params={"q": "London to Singapore"}).json()
    assert body["results"][0]["id"] == "SIA317"


def test_what_is_not_a_route():
    from app.routes_search import _places
    assert _places("SIN LHR") == ("SIN", "LHR")
    assert _places("HONG KONG TO KUALA LUMPUR") == ("HONG KONG", "KUALA LUMPUR")
    assert _places("FLIGHTS TO LONDON FROM SINGAPORE") == ("SINGAPORE", "LONDON")
    assert _places("FROM SIN → LHR") == ("SIN", "LHR")
    # registrations keep their dash; words with "to" in them are words
    for q in ("9V-SMA", "D-AIMA", "G-XLEA", "A6-EDA", "TOKYO", "TORONTO",
              "TOKYO HANEDA", "TO LONDON", "FROM SIN", "SIN TO", "FLIGHTS",
              "SIN TO LHR TO SYD", "A-B", "SIN-LHR-SYD", "→"):
        assert _places(q) in (None, ("TOKYO", "HANEDA")), q


def test_names_and_cities_without_their_accents(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    session.add(RefAirport(
        ident="SBGR", kind="large_airport", iso_country="BR", iata="GRU",
        name="São Paulo/Guarulhos–Governor André Franco Montoro "
             "International Airport", municipality="São Paulo"))
    session.add(RefAirport(ident="LSZH", kind="large_airport",
                           iso_country="CH", iata="ZRH",
                           name="Zürich Airport", municipality="Zurich"))
    session.add(RefAirline(icao="WIF", iata="WF", name="Widerøe"))
    session.commit(); session.close()
    for q, want in (("Sao Paulo", "GRU"), ("São Paulo", "GRU"),
                    ("sao paulo", "GRU"), ("Zurich", "ZRH"),
                    ("Zürich", "ZRH"), ("Wideroe", "WIF"),
                    ("Widerøe", "WIF")):
        body = client.get("/v1/search", params={"q": q}).json()
        assert body["results"] and body["results"][0]["id"] == want, q
    body = client.get("/v1/search", params={"q": "Sao Paulo"}).json()
    assert body["results"][0]["detail"] == "São Paulo · BR"
    assert body["results"][0]["label"].startswith("São Paulo/Guarulhos")


def test_a_busy_airport_outranks_an_air_base_named_first(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    session.add(RefAirport(ident="WSAC", name="Changi Air Base (East)",
                           kind="medium_airport", iso_country="SG",
                           municipality="Singapore", iata=None))
    session.add(RefSchedule(callsign="RSAF1", org="WSAC", dst="WSAP",
                            airline_icao="RSF", n_flights=280))
    for cs, org, dst, n in SCHEDULE:
        session.add(RefSchedule(callsign=cs, org=org, dst=dst,
                                airline_icao="SIA", n_flights=n))
    session.add(RefSchedule(callsign="SIA1", org="SIN", dst="SFO",
                            airline_icao="SIA", n_flights=9000))
    session.commit(); session.close()
    body = client.get("/v1/search", params={"q": "Changi"}).json()
    airports = [r["id"] for r in body["results"] if r["kind"] == "airport"]
    assert airports == ["SIN", "WSAC"]
    # the base still answers its own code first
    body = client.get("/v1/search", params={"q": "WSAC"}).json()
    assert body["results"][0]["id"] == "WSAC"


def test_other_spellings_redirect_to_the_canonical_query(ctx, tmp_path):
    client, _ = _ready(ctx, tmp_path)
    for raw, where in (
            ("singapore", "/v1/search?q=SINGAPORE"),
            ("  sq   322 ", "/v1/search?q=SQ%20322"),
            ("São Paulo", "/v1/search?q=S%C3%83O%20PAULO"),
            ("sin-lhr", "/v1/search?q=SIN-LHR")):
        resp = client.get("/v1/search", params={"q": raw},
                          follow_redirects=False)
        assert resp.status_code == 301, raw
        assert resp.headers["Location"] == where
        assert resp.headers["Cache-Control"] == "public, s-maxage=43200"
    # other parameters stay, in their order
    resp = client.get("/v1/search?x=1&q=sin&y=a+b", follow_redirects=False)
    assert resp.headers["Location"] == "/v1/search?x=1&q=SIN&y=a+b"
    # the canonical form answers as it is
    resp = client.get("/v1/search", params={"q": "SQ 322"},
                      follow_redirects=False)
    assert resp.status_code == 200
    assert client.get("/v1/search", params={"q": "s"}).status_code == 422


def test_the_fold_table_is_networkds():
    """networkd folds with the same table, or the two answer apart."""
    import os
    import re
    from app.routes_search import FOLD_FROM, FOLD_TO
    assert len(FOLD_FROM) == len(FOLD_TO)
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "..", "server", "crates", "networkd",
                            "src", "search.rs"), encoding="utf-8").read()
    for name, table in (("FOLD_FROM", FOLD_FROM), ("FOLD_TO", FOLD_TO)):
        body = re.search(r"const %s: &str = concat!\((.*?)\);" % name, src,
                         re.S).group(1)
        assert "".join(re.findall(r'"([^"]*)"', body)) == table, name
