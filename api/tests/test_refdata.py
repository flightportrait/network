"""Refdata importers (precedence merge, derive) and the public lookups."""
import gzip
import json

import pytest

from app import refdata_ingest
from app.refdata_models import (RefAirframe, RefAirlineCountry,
                                RefAllianceMembership, RefAirport, RefRoute)

TAR1090_LINES = (
    "76cd01;9V-SHA;A359;00;AIRBUS A-350-900;2019;Singapore Airlines;\n"
    "a12345;N26BD;ASTR;00;;1992;ARKANSAS BOLT CO;\n"
    "76cd02;9V-SHB;A359;00;AIRBUS A-350-900;;Singapore Airlines;\n"
    "badrow\n"
)

AIRPORTS_CSV = (
    '"id","ident","type","name","latitude_deg","longitude_deg",'
    '"elevation_ft","continent","iso_country","iso_region","municipality",'
    '"scheduled_service","icao_code","iata_code","gps_code","local_code",'
    '"home_link","wikipedia_link","keywords"\n'
    '1,"WSSS","large_airport","Singapore Changi",1.35019,103.994,22,"AS",'
    '"SG","SG-04","Singapore","yes","WSSS","SIN","WSSS",,,,\n'
    '2,"YSSY","large_airport","Sydney Kingsford Smith",-33.946,151.177,21,'
    '"OC","AU","AU-NSW","Sydney","yes","YSSY","SYD","YSSY",,,,\n'
    '3,"WMKK","large_airport","Kuala Lumpur Intl",2.745,101.71,69,"AS",'
    '"MY","MY-10","Sepang","yes","WMKK","KUL","WMKK",,,,\n'
)

SEED = {
    "airlines": {"SIA": {"name": "Singapore Airlines", "iata": "SQ",
                         "palette": ["#1D4886", "#FCB130"]}},
    "types": {"A359": {"name": "Airbus A350-900", "category": "wide"}},
}

# Chains are IATA codes — that is what the routes artifact
# carries; WMKK exercises the ICAO-ident fallback.
ROUTES = {"SIA211": ["SIN", "SYD"], "SIA345": ["SIN", "WMKK"],
          "9VABC": ["SIN", "KUL"]}

ALLIANCE_SEED = {
    "schema_version": 1,
    "as_of": "2026-08-29",
    "alliances": [
        {
            "slug": "star-alliance", "name": "Star Alliance",
            "website_url": "https://example.test/star",
            "source_url": "https://example.test/star/members",
            "source_checked_at": "2026-08-29",
            "memberships": [
                {"airline_icao": "SIA", "airline_iata": "SQ",
                 "airline_name": "Singapore Airlines", "status": "active",
                 "relationship": "member"},
                {"airline_icao": "PAL", "airline_iata": "PR",
                 "airline_name": "Planned Air", "status": "future",
                 "relationship": "member"},
            ],
        },
        {
            "slug": "oneworld", "name": "oneworld",
            "website_url": "https://example.test/oneworld",
            "source_url": "https://example.test/oneworld/members",
            "source_checked_at": "2026-08-29",
            "memberships": [
                {"airline_icao": "ASA", "airline_iata": "AS",
                 "airline_name": "Alaska Airlines", "status": "active",
                 "relationship": "member"},
                {"airline_icao": "HAL", "airline_iata": "HA",
                 "airline_name": "Hawaiian Airlines", "status": "active",
                 "relationship": "group-brand", "sponsor_icao": "ASA"},
            ],
        },
    ],
}


def _seed_all(sm, tmp_path):
    tar = tmp_path / "aircraft.csv.gz"
    tar.write_bytes(gzip.compress(TAR1090_LINES.encode()))
    airports = tmp_path / "airports.csv"
    airports.write_text(AIRPORTS_CSV)
    seed = tmp_path / "refdata_seed.json"
    seed.write_text(json.dumps(SEED))
    routes = tmp_path / "routes.json.gz"
    routes.write_bytes(gzip.compress(json.dumps(ROUTES).encode()))

    session = sm()
    try:
        assert refdata_ingest.ingest_tar1090(session, str(tar)) == 3
        assert refdata_ingest.ingest_airports(session, str(airports)) == 3
        assert refdata_ingest.ingest_seed(session, str(seed)) == 2
        assert refdata_ingest.ingest_routes(session, str(routes)) == 3
        refdata_ingest.derive(session)
        session.commit()
    finally:
        session.close()


def test_precedence_merge(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    try:
        # A lower-ranked artifact only fills silence: the tar1090
        # type/reg survive, but the observed operator (which tar1090
        # doesn't carry) and a hex only the dump knows both land.
        art = tmp_path / "airframes.json.gz"
        art.write_bytes(gzip.compress(json.dumps(
            {"76CD01": ["B77W", "WRONG", "SIA"],
             "3c6675": ["A388", "D-AIMK", None]}
        ).encode()))
        assert refdata_ingest.ingest_airframes(session, str(art)) == 2
        session.commit()

        kept = session.get(RefAirframe, "76cd01")
        assert kept.type_code == "A359" and kept.source == "tar1090"
        assert kept.operator_icao == "SIA"
        new = session.get(RefAirframe, "3c6675")
        assert new.type_code == "A388" and new.source == "fp-dump"

        # An override outranks and overwrites.
        refdata_ingest._merge_airframes(
            session, {"76cd01": {"type_code": "A35K"}}, "override")
        session.commit()
        fixed = session.get(RefAirframe, "76cd01")
        assert fixed.type_code == "A35K" and fixed.source == "override"
        assert fixed.registration == "9V-SHA"   # untouched fields survive
    finally:
        session.close()


def test_derive_airline_countries(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    try:
        # 9VABC is a registration-shaped callsign: never an airline route.
        assert session.get(RefRoute, "9VABC").airline_icao is None
        rows = session.query(RefAirlineCountry).all()
        got = {(r.airline_icao, r.iso_country): r.n_routes for r in rows}
        assert got == {("SIA", "SG"): 2, ("SIA", "AU"): 1, ("SIA", "MY"): 1}
    finally:
        session.close()


def _make_legs_db(path, generated="2026-08-12"):
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE legs (hex TEXT, reg TEXT, type TEXT, "
                 "date TEXT, org TEXT, dst TEXT, dep_ts INT, arr_ts INT, "
                 "max_alt INT)")
    conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    conn.executemany("INSERT INTO meta VALUES (?,?)",
                     [("window_days", "60"), ("generated_at", generated)])
    # 76cd01 is operator SIA (from the airframes artifact). Seven SIN-SYD
    # legs across days, mixed A359/A388; one SIN-KUL. dep/arr 1000s apart
    # so all clear the >=600s duration floor.
    rows = []
    for day in range(7):
        rows.append(("76cd01", "9V-SHA", "A359" if day % 2 else "A388",
                     "2026-08-%02d" % (5 + day), "SIN", "SYD",
                     1000, 2000, 40000))
    rows.append(("76cd01", "9V-SHA", "A359", "2026-08-05", "SIN", "KUL",
                 1000, 6000, 39000))
    # A 100s touch-and-go on an already-counted day: must be dropped by the
    # duration floor, so SIN-SYD stays 7 flights / A388 stays 4.
    rows.append(("76cd01", "9V-SHA", "A388", "2026-08-06", "SIN", "SYD",
                 1000, 1100, 40000))
    conn.executemany("INSERT INTO legs VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_airport_tz(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    # OpenFlights rows: ...,IATA,ICAO,...,Tz(idx 11),...
    of = tmp_path / "openflights.dat"
    of.write_text(
        '1,"Changi","Singapore","SG","SIN","WSSS",1.35,103.9,22,8,"N",'
        '"Asia/Singapore","airport","OurAirports"\n'
        '2,"Sydney","Sydney","AU","SYD","YSSY",-33.9,151.1,21,10,"N",'
        '"Australia/Sydney","airport","OurAirports"\n')
    session = sm()
    try:
        n = refdata_ingest.ingest_airport_tz(session, str(of))
        session.commit()
        assert n == 2
        assert session.get(RefAirport, "WSSS").tz == "Asia/Singapore"
    finally:
        session.close()
    # tz rides along in the routes airports payload
    body = client.get("/v1/airlines/SIA/routes").json()
    assert body["airports"]["SIN"]["tz"] == "Asia/Singapore"


def test_leg_stats_and_flightlog_routes(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    try:
        # 76cd01 needs operator_icao=SIA for the hex->airline join.
        refdata_ingest._merge_airframes(
            session, {"76cd01": {"operator_icao": "SIA"}}, "fp-dump")
        session.commit()
    finally:
        session.close()

    legs_db = tmp_path / "legs.db"
    _make_legs_db(legs_db, generated="2026-08-12")   # 7 days since floor
    session = sm()
    try:
        assert refdata_ingest.ingest_leg_stats(session, str(legs_db)) == 2
        session.commit()
    finally:
        session.close()

    body = client.get("/v1/airlines/SIA/routes").json()
    assert body["source"] == "flightlog"
    legs = {(l["org"], l["dst"]): l for l in body["legs"]}
    sinsyd = legs[("SIN", "SYD")]
    assert sinsyd["n"] == 7 and sinsyd["days"] == 7
    # 7 flights / 7 observed days * 7 = 7.0 per week
    assert sinsyd["per_week"] == 7.0
    # aircraft resolved to names, most-flown first
    ac = {a["type"]: a for a in sinsyd["aircraft"]}
    assert ac["A388"]["n"] == 4 and ac["A359"]["n"] == 3   # days 0,2,4,6 A388
    assert sinsyd["aircraft"][0]["type"] == "A388"            # most-flown first

    # The per-route airframes endpoint: the actual tail, its reg and type.
    leg = client.get("/v1/airlines/SIA/leg/SIN/SYD").json()
    assert leg["flights"] == 7                             # touch-and-go dropped
    af = leg["airframes"]
    assert af[0]["hex"] == "76cd01" and af[0]["reg"] == "9V-SHA"
    assert af[0]["type"] == "A359" and af[0]["flights"] == 7
    # undirected: endpoints in either order resolve the same row
    assert client.get("/v1/airlines/SIA/leg/SYD/SIN").json()["flights"] == 7
    assert client.get("/v1/airlines/SIA/leg/SIN/JFK").status_code == 404
    assert ac["A359"]["name"] == "Airbus A350-900"

    # A missing meta table reads as mid-delivery, not a silent empty.
    import sqlite3
    broken = tmp_path / "broken.db"
    c = sqlite3.connect(str(broken))
    c.execute("CREATE TABLE legs (hex TEXT)")
    c.commit(); c.close()
    session = sm()
    try:
        import pytest as _pytest
        with _pytest.raises(SystemExit):
            refdata_ingest.ingest_leg_stats(session, str(broken))
    finally:
        session.close()


def _make_sched_legs_db(path):
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE legs (hex TEXT, reg TEXT, type TEXT, "
                 "callsign TEXT, date TEXT, org TEXT, dst TEXT, dep_ts INT, "
                 "arr_ts INT, max_alt INT)")
    conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    conn.executemany("INSERT INTO meta VALUES (?,?)",
                     [("window_days", "60"), ("generated_at", "2026-08-12")])
    # SIA322 SIN->SYD: WSSS is Asia/Singapore (UTC+8). Two obs of the same
    # ~01:30 UTC departure = 09:30 local; one jittered a few min.
    rows = [
        ("76cd01", "9V-SHA", "A359", "SIA322", "2026-08-05", "SIN", "SYD",
         1754443800, 1754470800, 40000),   # 2026-08-06 01:30 UTC -> 09:30 SGT
        ("76cd01", "9V-SHA", "A359", "SIA322", "2026-08-06", "SIN", "SYD",
         1754530260, 1754557260, 40000),   # 01:31 UTC -> 09:31 SGT
        ("76cd02", "9V-SHB", "A388", "SIA322", "2026-08-07", "SIN", "SYD",
         1754616600, 1754643600, 40000),   # 01:30 UTC -> 09:30 SGT, A388
    ]
    conn.executemany("INSERT INTO legs VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_schedule(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    session = sm()
    try:
        session.get(RefAirport, "WSSS").tz = "Asia/Singapore"
        session.commit()
    finally:
        session.close()

    legs_db = tmp_path / "sched.db"
    _make_sched_legs_db(legs_db)
    session = sm()
    try:
        assert refdata_ingest.ingest_schedule(session, str(legs_db)) == 1
        session.commit()
    finally:
        session.close()

    settings.schedule_min_flights = 1     # tiny fixture, gate aside
    body = client.get("/v1/airlines/SIA/schedule/SIN/SYD").json()
    deps = body["departures"]
    assert len(deps) == 1
    dep = deps[0]
    assert dep["callsign"] == "SIA322"
    assert dep["flight"] == "SQ322"          # ICAO SIA -> IATA SQ
    assert dep["dep"] == "09:30"             # local mode, DST-correct
    assert dep["type"] == "A359"             # 2 of 3 legs
    assert dep["n_flights"] == 3


def test_lookup_endpoints(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)

    body = client.get("/v1/airframes/76CD01").json()
    assert body["reg"] == "9V-SHA"
    assert body["type_name"] == "Airbus A350-900"

    body = client.get("/v1/airlines/sia").json()
    assert body["name"] == "Singapore Airlines"
    assert body["n_routes"] == 2

    roster = client.get("/v1/airlines").json()["airlines"]
    assert roster[0]["icao"] == "SIA"
    assert roster[0]["n_routes"] == 2 and roster[0]["n_countries"] == 3

    body = client.get("/v1/airlines/SIA/countries").json()
    assert body["countries"][0] == {"iso_country": "SG", "n_routes": 2}

    body = client.get("/v1/airlines/SIA/routes").json()
    assert body["source"] == "chains"           # no flight log ingested here
    assert set(body["airports"]) >= {"SIN", "SYD"}
    assert body["airports"]["SIN"]["iso_country"] == "SG"
    legs = {(l["org"], l["dst"]): l["n"] for l in body["legs"]}
    assert legs.get(("SIN", "SYD")) == 1        # SIA211, undirected, sorted

    # Fleet counting through the observed-operator path.
    session = sm()
    from app import refdata_ingest as ri
    art = tmp_path / "af.json.gz"
    art.write_bytes(gzip.compress(json.dumps(
        {"76cd01": ["A359", "9V-SHA", "SIA"],
         "76cd02": ["A359", "9V-SHB", "SIA"],
         "76cd09": ["B77W", "9V-SWZ", "SIA"]}).encode()))
    ri.ingest_airframes(session, str(art))
    session.commit()
    session.close()

    drill = client.get("/v1/airlines/SIA/fleet/A359").json()
    assert drill["type"] == "A359"
    assert [a["reg"] for a in drill["airframes"]] and \
        all(a["hex"] for a in drill["airframes"])
    assert client.get(
        "/v1/airlines/SIA/fleet/ZZZZ").status_code == 404
    body = client.get("/v1/airlines/SIA/fleet").json()
    assert body["fleet"][0] == {"type": "A359",
                                "type_name": "Airbus A350-900", "count": 2}
    assert body["fleet"][1] == {"type": "B77W", "type_name": None,
                                "count": 1}
    assert body["n_airframes"] == 3

    body = client.get("/v1/airframes/76cd09").json()
    assert body["operator_icao"] == "SIA"
    assert body["airline"]["name"] == "Singapore Airlines"

    assert client.get("/v1/types/A359").json()["category"] == "wide"
    body = client.get("/v1/airports/WSSS").json()
    assert body["iso_country"] == "SG" and body["iata"] == "SIN"

    resp = client.get("/v1/airports/WSSS")
    assert resp.headers["Cache-Control"] == "public, s-maxage=3600"


def test_alliance_ingest_and_coverage_endpoints(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    _seed_all(sm, tmp_path)
    path = tmp_path / "alliances.json"
    path.write_text(json.dumps(ALLIANCE_SEED))
    session = sm()
    try:
        assert refdata_ingest.ingest_alliances(session, str(path)) == 6
        session.commit()
        # Whole-snapshot replacement is idempotent, not append-only.
        assert refdata_ingest.ingest_alliances(session, str(path)) == 6
        session.commit()
        assert session.query(RefAllianceMembership).count() == 4
    finally:
        session.close()

    airline = client.get("/v1/airlines/SIA").json()
    assert airline["alliances"] == [{
        "slug": "star-alliance", "name": "Star Alliance",
        "status": "active", "relationship": "member",
        "sponsor_icao": None, "effective_from": None,
        "effective_to": None,
        "source_url": "https://example.test/star/members",
        "source_checked_at": "2026-08-29",
    }]

    roster = client.get("/v1/alliances").json()["alliances"]
    by_slug = {row["slug"]: row for row in roster}
    star = by_slug["star-alliance"]
    assert star["n_members"] == 1 and star["n_members_observed"] == 1
    assert star["n_routes"] == 2 and star["n_countries"] == 3
    assert star["n_flights"] == 0 and star["n_legs"] == 0
    oneworld = by_slug["oneworld"]
    assert oneworld["n_members"] == 1 and oneworld["n_airlines"] == 2
    assert oneworld["n_members_observed"] == 0

    detail = client.get("/v1/alliances/star-alliance").json()
    statuses = {m["airline"]["icao"]: m["status"]
                for m in detail["memberships"]}
    assert statuses == {"SIA": "active", "PAL": "future"}
    assert detail["countries"][0] == {"iso_country": "SG", "n_routes": 2}

    routes = client.get(
        "/v1/alliances/star-alliance/routes").json()
    assert routes["source"] == "chains"
    legs = {(leg["org"], leg["dst"]): leg["n"] for leg in routes["legs"]}
    assert legs[("SIN", "SYD")] == 1

    assert client.get("/v1/alliances/nope").status_code == 404
    assert client.get(
        "/v1/alliances/nope/routes").status_code == 404


def test_alliance_ingest_rejects_unbacked_affiliate(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    bad = json.loads(json.dumps(ALLIANCE_SEED))
    bad["alliances"][1]["memberships"][1]["sponsor_icao"] = "XXX"
    path = tmp_path / "bad-alliances.json"
    path.write_text(json.dumps(bad))
    session = sm()
    try:
        with pytest.raises(ValueError, match="not an active direct member"):
            refdata_ingest.ingest_alliances(session, str(path))
    finally:
        session.close()


def test_unknowns_404(ctx):
    client, app, sm, settings, readsb = ctx
    for path in ("/v1/airlines/XXX",
                 "/v1/airlines/XXX/routes",
                 "/v1/airlines/XXX/countries",
                 "/v1/airlines/XXX/fleet",
                 "/v1/alliances/XXX",
                 "/v1/types/ZZZZ",
                 "/v1/airports/ZZZZ"):
        assert client.get(path).status_code == 404


def test_refdata_rate_limit_bucket(ctx):
    client, app, sm, settings, readsb = ctx
    settings.refdata_rate_limit = 2
    assert client.get("/v1/airlines").status_code == 200
    assert client.get("/v1/airlines").status_code == 200
    assert client.get("/v1/airlines").status_code == 429
    # Its own bucket: the live sky is not starved by refdata traffic.
    assert client.get("/v1/now").status_code == 200


def test_airline_names_completion(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    from app import refdata_ingest
    from app.refdata_models import RefAirline, RefSchedule
    dat = tmp_path / "airlines.dat"
    dat.write_text(
        '1,"Delta Air Lines",\\N,"DL","DAL","DELTA","United States","Y"\n'
        '2,"Ghost Air",\\N,"GH","GHO","GHOST","Nowhere","N"\n'
        '3,"Bad Row"\n')
    session = sm()
    try:
        session.add(RefSchedule(callsign="DAL22", org="DTW", dst="MUC",
                                airline_icao="DAL", dep_min=0, arr_min=0,
                                type_code="A333", n_flights=30))
        session.add(RefSchedule(callsign="ZZZ1", org="AAA", dst="BBB",
                                airline_icao="ZZZ", dep_min=0, arr_min=0,
                                type_code=None, n_flights=30))
        session.commit()
        added = refdata_ingest.ingest_airline_names(session, str(dat))
        session.commit()
        assert added == 1                 # DAL named; ZZZ unknown, left out
        row = session.get(RefAirline, "DAL")
        assert row.name == "Delta Air Lines" and row.iata == "DL"
        assert row.palette == []
        assert session.get(RefAirline, "ZZZ") is None
    finally:
        session.close()


def test_boards_merge(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    import sqlite3 as s3
    from app import refdata_ingest
    from app.refdata_models import RefAirline, RefSchedule
    bdb = tmp_path / "boards.db"
    conn = s3.connect(str(bdb))
    conn.execute("CREATE TABLE boards (airport TEXT, kind TEXT,"
                 " flight TEXT, counterpart TEXT, sched_min INT,"
                 " day TEXT, source TEXT, fetched_at TEXT)")
    rows = [
        # matches the observed OSL->EWR service below within 45 min
        ("OSL", "dep", "SK907", "EWR", 660, "2026-08-29", "t", "now"),
        # no observed counterpart: extends coverage as published-only
        ("OSL", "dep", "WF569", "FRO", 500, "2026-08-29", "t", "now"),
        ("OSL", "dep", "WF569", "FRO", 500, "2026-08-30", "t", "now"),
        # arrival fills the matched row's missing arr_min
        ("EWR", "arr", "SK907", "OSL", 800, "2026-08-29", "t", "now"),
    ]
    conn.executemany("INSERT INTO boards VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    session = sm()
    try:
        session.add(RefAirline(icao="SAS", iata="SK",
                               name="Scandinavian", palette=[]))
        session.add(RefSchedule(callsign="SAS43C", org="OSL", dst="EWR",
                                airline_icao="SAS", dep_min=655,
                                arr_min=None, type_code="B78X",
                                n_flights=50))
        session.commit()
        refdata_ingest.ingest_boards(session, str(bdb))
        session.commit()
        decorated = session.get(RefSchedule, ("SAS43C", "OSL", "EWR"))
        assert decorated.flight == "SK907" and decorated.source == "both"
        assert decorated.dep_min == 655            # observation stays
        assert decorated.arr_min == 800            # arrival filled
        added = session.get(RefSchedule, ("WF569", "OSL", "FRO"))
        assert added.source == "published"
        assert added.dep_min == 500 and added.n_flights == 2
        # graceful skip when no artifact exists
        assert refdata_ingest.ingest_boards(
            session, str(tmp_path / "absent.db")) == 0
    finally:
        session.close()
