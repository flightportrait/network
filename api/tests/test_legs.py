"""The merged history resources: /v1/airframes, /v1/airports,
/v1/flights — artifact lookup, identity, section nullability, 404s,
feature-dark 503s, reload."""
import sqlite3

from app.legs_db import LegBook
from app.routes_db import RouteBook


def _build(path, rows, window_days=60):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE legs (hex TEXT, reg TEXT, type TEXT,"
                 " callsign TEXT, date TEXT, org TEXT, dst TEXT,"
                 " dep_ts INT, arr_ts INT, max_alt INT)")
    conn.execute("CREATE INDEX ix_legs_hex ON legs(hex)")
    conn.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    conn.execute("INSERT INTO meta VALUES ('window_days', ?)",
                 (str(window_days),))
    conn.executemany("INSERT INTO legs VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


LEGS = [
    ("76cd06", "9V-SHF", "A359", "SQ322", "2026-08-27", "SIN", "LHR",
     1787800000, 1787845000, 41000),
    ("76cd06", "9V-SHF", "A359", "SQ317", "2026-08-26", "LHR", "SIN",
     1787700000, 1787747000, 40000),
    ("76cd06", None, None, None, "2026-08-20", "SIN", "CDG",
     1787200000, 1787247000, 40000),
]


def test_airframe_log(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    db = tmp_path / "legs.db"
    _build(str(db), LEGS)
    app.state.legs = LegBook(str(db))
    body = client.get("/v1/airframes/76CD06").json()   # case-insensitive
    assert body["reg"] == "9V-SHF" and body["type"] == "A359"
    assert body["window_days"] == 60                   # an int, not "60"
    assert body["coverage"] == "observed"
    assert [l["date"] for l in body["legs"]] == \
        ["2026-08-27", "2026-08-26", "2026-08-20"]
    assert body["legs"][0]["org"] == "SIN"
    assert body["legs"][0]["callsign"] == "SQ322"
    assert body["country"] == "SG"                     # address block


def test_state_of_address_block():
    from app.address_blocks import BLOCKS, state_of
    assert state_of("49d283") == "CZ"
    assert state_of("A00001") == "US"
    # nested blocks: the smallest wins
    assert state_of("400001") == "BM"         # inside the UK block
    assert state_of("4001c5") == "KY"
    assert state_of("406a3b") == "GB"
    assert state_of("43eaf1") == "IM"         # Isle of Man's own block
    assert state_of("789123") == "HK"         # inside China's
    assert state_of("780001") == "CN"
    assert state_of("f00001") is None         # ICAO temporary
    assert state_of("000001") is None         # unallocated
    assert state_of("xyz") is None
    assert [b[0] for b in BLOCKS] == sorted(b[0] for b in BLOCKS)


def test_airframe_merges_registry(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    from app.refdata_models import RefAirframe, RefAirline, RefType
    db = tmp_path / "legs.db"
    _build(str(db), LEGS)
    app.state.legs = LegBook(str(db))
    with sm() as session:
        session.add(RefAirframe(hex="76cd06", registration="9V-SHF",
                                type_code="A359", operator_icao="SIA",
                                year=2019, source="tar1090"))
        session.add(RefAirline(icao="SIA", iata="SQ",
                               name="Singapore Airlines", palette=[]))
        session.add(RefType(designator="A359", name="Airbus A350-900",
                            category="wide"))
        session.commit()
    body = client.get("/v1/airframes/76cd06").json()
    assert body["operator"] == "Singapore Airlines"
    assert body["airline"]["iata"] == "SQ"
    assert body["type_name"] == "Airbus A350-900"
    assert body["category"] == "wide"
    assert body["year"] == 2019
    assert len(body["legs"]) == 3

    # registry-only hex: known identity, empty (not null) log
    with sm() as session:
        session.add(RefAirframe(hex="abc123", registration="N123AB",
                                type_code=None, source="tar1090"))
        session.commit()
    body = client.get("/v1/airframes/abc123").json()
    assert body["reg"] == "N123AB"
    assert body["legs"] == [] and body["window_days"] == 60


def test_airframe_unknown_404_and_invalid_422(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    db = tmp_path / "legs.db"
    _build(str(db), LEGS)
    app.state.legs = LegBook(str(db))
    response = client.get("/v1/airframes/abcdef")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/v1/airframes/zzzzzz").status_code == 422
    assert client.get("/v1/airframes/abc").status_code == 422


def test_airframe_dark_artifact(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    app.state.legs = LegBook(str(tmp_path / "absent.db"))
    # nothing anywhere: unknowable, not "unknown airframe"
    response = client.get("/v1/airframes/76cd06")
    assert response.status_code == 503
    assert response.json()["error"] == "artifact_unavailable"
    assert response.headers["retry-after"] == "300"
    # registry knows it: identity served, log null
    from app.refdata_models import RefAirframe
    with sm() as session:
        session.add(RefAirframe(hex="76cd06", registration="9V-SHF",
                                type_code="A359", source="tar1090"))
        session.commit()
    body = client.get("/v1/airframes/76cd06").json()
    assert body["reg"] == "9V-SHF"
    assert body["legs"] is None and body["window_days"] is None


def test_reload_on_mtime_change(tmp_path):
    import os
    db = tmp_path / "legs.db"
    _build(str(db), LEGS[:1])
    book = LegBook(str(db), reload_s=0.0)
    assert len(book.get("76cd06")["legs"]) == 1
    os.remove(db)
    _build(str(db), LEGS)
    os.utime(db, (0, 9999999999))
    assert len(book.get("76cd06")["legs"]) == 3


# ---- airport view ---------------------------------------------------

import datetime as _dt


def _utc(day, hour, minute=0):
    return int(_dt.datetime(2026, 8, day, hour, minute,
                            tzinfo=_dt.timezone.utc).timestamp())


AIRPORT_LEGS = [
    # SQ322 SIN->LHR at 08:00 UTC, three days running
    ("76cd06", "9V-SHF", "A359", "SQ322", "2026-08-25", "SIN", "LHR",
     _utc(25, 8), None, 41000),
    ("76cd07", "9V-SHG", "A359", "SQ322", "2026-08-26", "SIN", "LHR",
     _utc(26, 8), None, 41000),
    ("76cd06", "9V-SHF", "A359", "SQ322", "2026-08-27", "SIN", "LHR",
     _utc(27, 8), None, 41000),
    # TR604 SIN->KUL twice, 02:30 UTC
    ("76a001", "9V-TRA", "B38M", "TR604", "2026-08-26", "SIN", "KUL",
     _utc(26, 2, 30), None, 20000),
    ("76a001", "9V-TRA", "B38M", "TR604", "2026-08-27", "SIN", "KUL",
     _utc(27, 2, 30), None, 20000),
    # a one-off charter: stays off the board, still counts in stats
    ("76b111", None, "GLF6", "XOJ77", "2026-08-27", "SIN", "HND",
     _utc(27, 1), None, 45000),
    # an arrival only: never a SIN departure
    ("76c222", None, "B77W", "BAW11", "2026-08-27", "LHR", "SIN",
     _utc(27, 0), None, 40000),
]


def _add_sin_registry(sm):
    from app.refdata_models import RefAirport
    with sm() as session:
        session.add(RefAirport(ident="WSSS", name="Singapore Changi",
                               kind="large_airport", lat=1.35019,
                               lon=103.994, iso_country="SG",
                               municipality="Singapore", iata="SIN",
                               tz="Asia/Singapore"))
        session.commit()


def test_airport_board(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    db = tmp_path / "legs.db"
    # a circuit rides along: excluded from stats and routes
    _build(str(db), AIRPORT_LEGS + [
        ("76d999", None, "P28A", "TRAIN1", "2026-08-27", "SIN", "SIN",
         _utc(27, 5), None, 2000)])
    from app.refdata_models import RefSchedule
    app.state.legs = LegBook(str(db))
    _add_sin_registry(sm)
    with sm() as session:
        session.add(RefSchedule(callsign="SIA322", org="SIN", dst="LHR",
                                airline_icao="SIA", dep_min=16 * 60,
                                arr_min=22 * 60 + 35, type_code="A359",
                                n_flights=30))
        session.add(RefSchedule(callsign="TGW604", org="SIN", dst="KUL",
                                airline_icao="TGW", dep_min=10 * 60 + 30,
                                arr_min=11 * 60 + 30, type_code="B38M",
                                n_flights=20))
        session.add(RefSchedule(callsign="SIA999", org="KUL", dst="SIN",
                                airline_icao="SIA", dep_min=9 * 60,
                                arr_min=None, type_code="A359",
                                n_flights=90))          # other airport
        session.commit()
    body = client.get("/v1/airports/sin").json()       # case-insensitive
    assert body["iata"] == "SIN" and body["ident"] == "WSSS"
    assert body["name"] == "Singapore Changi"
    assert body["tz"] == "Asia/Singapore"
    assert body["observed"]["departures"] == 6         # arrivals+circuit out
    assert body["observed"]["destinations"] == 3
    assert body["window_days"] == 60
    assert body["times"] == "local"
    assert body["coverage"] == "observed"
    # the board comes from the schedule table, local times, dep-sorted
    assert [b["flight"] for b in body["board"]] == ["TGW604", "SIA322"]
    sq = body["board"][1]
    assert sq["dst"] == "LHR" and sq["type"] == "A359"
    assert sq["dep"] == "16:00" and sq["arr"] == "22:35"
    assert body["observed"]["routes"][0]["dst"] == "LHR"
    assert "SIN" not in [r["dst"] for r in body["observed"]["routes"]]
    # arrivals: the legs that end here, in this airport's local time
    assert [b["flight"] for b in body["arrivals"]] == ["SIA999"]
    assert body["arrivals"][0]["org"] == "KUL"
    assert body["arrivals"][0]["dep"] == "09:00"
    assert body["airlines"][0] == {"icao": "SIA", "flights": 30}
    # the ICAO spelling lands on the same airport
    via_icao = client.get("/v1/airports/WSSS").json()
    assert via_icao["iata"] == "SIN"
    assert via_icao["observed"]["departures"] == 6


def test_airport_unknown_invalid_and_dark(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    db = tmp_path / "legs.db"
    _build(str(db), AIRPORT_LEGS)
    app.state.legs = LegBook(str(db))
    response = client.get("/v1/airports/ZZZ")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert client.get("/v1/airports/to").status_code == 422
    assert client.get("/v1/airports/abc:de").status_code == 422
    # observed-only airport (not in the registry): still served
    body = client.get("/v1/airports/SIN").json()
    assert body["iata"] == "SIN" and body["ident"] is None
    assert body["observed"]["departures"] == 6
    # log dark and not in the registry: unknowable, 503
    app.state.legs = LegBook(str(tmp_path / "absent.db"))
    response = client.get("/v1/airports/SIN")
    assert response.status_code == 503
    assert response.json()["error"] == "artifact_unavailable"
    # log dark but the registry knows it: identity with observed null
    _add_sin_registry(sm)
    body = client.get("/v1/airports/SIN").json()
    assert body["ident"] == "WSSS"
    assert body["observed"] is None and body["window_days"] is None


def test_flight_view(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    db = tmp_path / "legs.db"
    _build(str(db), AIRPORT_LEGS)
    from app.refdata_models import RefSchedule
    app.state.legs = LegBook(str(db))
    app.state.routes = RouteBook(str(tmp_path / "absent.json.gz"))
    with sm() as session:
        session.add(RefSchedule(callsign="SQ322", org="SIN", dst="LHR",
                                airline_icao="SQ3", dep_min=16 * 60,
                                arr_min=22 * 60 + 35, type_code="A359",
                                n_flights=30))
        session.commit()
    body = client.get("/v1/flights/sq322").json()      # case-insensitive
    assert body["callsign"] == "SQ322"
    assert body["route"] is None                       # routes artifact dark
    assert body["window_days"] == 60
    assert body["coverage"] == "observed"
    leg = body["legs"][0]
    assert leg["org"] == "SIN" and leg["dst"] == "LHR"
    assert leg["flights"] == 3 and leg["days"] == 3
    assert leg["dep"] == "16:00" and leg["arr"] == "22:35"
    assert leg["type"] == "A359"
    assert leg["times"] == "observed" and leg["flight"] is None
    regs = [a["reg"] for a in body["aircraft"]]
    assert "9V-SHF" in regs and "9V-SHG" in regs
    assert body["aircraft"][0]["flights"] == 2        # busiest tail first
    assert len(body["recent"]) == 3
    assert body["recent"][0]["date"] == "2026-08-27"
    response = client.get("/v1/flights/ZZZZ9")
    assert response.status_code == 404
    assert response.json()["error"] == "not_observed"
    assert client.get("/v1/flights/a").status_code == 422


def test_flight_dark_when_all_sources_missing(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    app.state.legs = LegBook(str(tmp_path / "absent.db"))
    app.state.routes = RouteBook(str(tmp_path / "absent.json.gz"))
    response = client.get("/v1/flights/SQ322")
    assert response.status_code == 503
    assert response.json()["error"] == "artifact_unavailable"


def test_flight_tail_count_excludes_circuits(ctx, tmp_path):
    # a tail's flight count must exclude circuits and one-sided legs,
    # the same predicate legs/recent use
    client, app, sm, settings, readsb = ctx
    db = tmp_path / "legs.db"
    _build(str(db), [
        ("aa1111", "N1", "A320", "TST100", "2026-08-25", "SIN", "KUL",
         _utc(25, 8), None, 30000),
        ("aa1111", "N1", "A320", "TST100", "2026-08-26", "SIN", "KUL",
         _utc(26, 8), None, 30000),
        ("aa1111", "N1", "A320", "TST100", "2026-08-27", "SIN", "SIN",
         _utc(27, 8), None, 5000),        # circuit — must not count
        ("aa1111", "N1", "A320", "TST100", "2026-08-28", "SIN", None,
         _utc(28, 8), None, 30000),       # one-sided — must not count
    ])
    app.state.legs = LegBook(str(db))
    body = client.get("/v1/flights/TST100").json()
    tail = body["aircraft"][0]
    assert tail["hex"] == "aa1111"
    assert tail["flights"] == 2          # not 4


def test_airframe_summary_where_it_sits(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    db = tmp_path / "legs.db"
    _build(str(db), LEGS)
    book = LegBook(str(db))
    app.state.legs = book
    s = book.airframe_summary("76CD06")
    assert s["legs"] == 3 and s["last_date"] == "2026-08-27"
    # the last leg landed at LHR with its arrival observed: that is where it sits
    assert s["last_org"] == "SIN" and s["last_dst"] == "LHR" and s["where"] == "LHR"
    assert s["top_route"][2] == 1 and s["top_route"][0] in ("SIN", "LHR")
    assert book.airframe_summary("000000") is None


def test_now_reports_archive_through(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    assert client.get("/v1/now").json()["archive_through"] is None
    db = tmp_path / "legs.db"
    _build(str(db), LEGS)
    app.state.legs = LegBook(str(db))
    assert client.get("/v1/now").json()["archive_through"] == "2026-08-27"


def test_routes_bulk(ctx, tmp_path):
    import gzip
    import json
    client, app, sm, settings, readsb = ctx
    db = tmp_path / "legs.db"
    _build(str(db), AIRPORT_LEGS)
    app.state.legs = LegBook(str(db))
    with gzip.open(tmp_path / "routes.json.gz", "wt") as fh:
        json.dump({"sq322": ["SIN", "LHR"], "BAW9": ["LHR", "SIN"]}, fh)
    app.state.routes = RouteBook(str(tmp_path / "routes.json.gz"))
    body = client.get("/v1/routes?cs=sq322, baw9,ZZZZ9,sq322").json()
    assert body == {"routes": {"SQ322": ["SIN", "LHR"], "BAW9": ["LHR", "SIN"],
                               "ZZZZ9": None}}
    assert client.get("/v1/routes?cs=a").status_code == 422
    assert client.get("/v1/routes?cs=" + ",".join(
        "AB%03d" % i for i in range(81))).status_code == 422
    # the list's bucket is not the tap's bucket
    for _ in range(3):
        client.get("/v1/routes?cs=SQ322")
    assert client.get("/v1/flights/sq322").status_code == 200


def test_boards_pin_only_the_same_airline(ctx, tmp_path):
    import sqlite3 as s3
    from app import refdata_ingest
    from app.refdata_models import RefAirline, RefSchedule
    client, app, sm, settings, readsb = ctx
    bdb = tmp_path / "boards.db"
    conn = s3.connect(str(bdb))
    conn.execute("CREATE TABLE boards (airport TEXT, kind TEXT, flight TEXT,"
                 " counterpart TEXT, sched_min INT, day TEXT, source TEXT,"
                 " fetched_at TEXT)")
    conn.execute("INSERT INTO boards VALUES ('DUB','dep','EI172','LHR',955,"
                 "'2026-09-22','t','now')")
    conn.commit(); conn.close()
    session = sm()
    try:
        session.add(RefAirline(icao="EIN", iata="EI", name="Aer Lingus", palette=[]))
        session.add(RefAirline(icao="BAW", iata="BA", name="British Airways", palette=[]))
        # British Airways five minutes off, Aer Lingus ten minutes off:
        # the number belongs to Aer Lingus
        session.add(RefSchedule(callsign="BAW831", org="DUB", dst="LHR",
                                airline_icao="BAW", dep_min=950, arr_min=1005,
                                type_code=None, n_flights=49, flight="EI172",
                                source="both"))          # an old, wrong pin
        session.add(RefSchedule(callsign="EIN172", org="DUB", dst="LHR",
                                airline_icao="EIN", dep_min=965, arr_min=1030,
                                type_code=None, n_flights=50))
        session.commit()
        refdata_ingest.ingest_boards(session, str(bdb))
        session.commit()
        ba = session.get(RefSchedule, ("BAW831", "DUB", "LHR"))
        ei = session.get(RefSchedule, ("EIN172", "DUB", "LHR"))
        assert ba.flight is None and ba.source == "observed"
        assert ei.flight == "EI172" and ei.source == "both"
        assert ei.dep_min == 955                        # the board's time now
    finally:
        session.close()


def test_boards_pin_the_callsign_with_the_same_number_first(ctx, tmp_path):
    import sqlite3 as s3
    from app import refdata_ingest
    from app.refdata_models import RefAirline, RefSchedule
    client, app, sm, settings, readsb = ctx
    bdb = tmp_path / "boards.db"
    conn = s3.connect(str(bdb))
    conn.execute("CREATE TABLE boards (airport TEXT, kind TEXT, flight TEXT,"
                 " counterpart TEXT, sched_min INT, day TEXT, source TEXT,"
                 " fetched_at TEXT)")
    conn.execute("INSERT INTO boards VALUES ('MCO','dep','AA1630','MIA',600,"
                 "'2026-09-22','t','now')")
    conn.commit(); conn.close()
    session = sm()
    try:
        session.add(RefAirline(icao="AAL", iata="AA", name="American", palette=[]))
        # a shuttle leg: another American service five minutes off, the
        # one carrying the number an hour off
        session.add(RefSchedule(callsign="AAL9790", org="MCO", dst="MIA",
                                airline_icao="AAL", dep_min=605, arr_min=665,
                                type_code=None, n_flights=20))
        session.add(RefSchedule(callsign="AAL1630", org="MCO", dst="MIA",
                                airline_icao="AAL", dep_min=660, arr_min=720,
                                type_code=None, n_flights=40))
        session.commit()
        refdata_ingest.ingest_boards(session, str(bdb))
        session.commit()
        other = session.get(RefSchedule, ("AAL9790", "MCO", "MIA"))
        same = session.get(RefSchedule, ("AAL1630", "MCO", "MIA"))
        assert other.flight is None
        assert same.flight == "AA1630" and same.source == "both"
        assert same.dep_min == 600
    finally:
        session.close()


def test_a_board_without_counterparts_still_names_the_service(ctx, tmp_path):
    import sqlite3 as s3
    from app import refdata_ingest
    from app.refdata_models import RefAirline, RefSchedule
    client, app, sm, settings, readsb = ctx
    bdb = tmp_path / "boards.db"
    conn = s3.connect(str(bdb))
    conn.execute("CREATE TABLE boards (airport TEXT, kind TEXT, flight TEXT,"
                 " counterpart TEXT, sched_min INT, day TEXT, source TEXT,"
                 " fetched_at TEXT)")
    conn.execute("INSERT INTO boards VALUES ('LGW','dep','EZY8124',NULL,1055,"
                 "'2026-09-22','t','now')")
    conn.commit(); conn.close()
    session = sm()
    try:
        session.add(RefAirline(icao="EZY", iata="U2", name="easyJet", palette=[]))
        session.add(RefSchedule(callsign="EZY8124", org="LGW", dst="ACE",
                                airline_icao="EZY", dep_min=1050, arr_min=1310,
                                type_code=None, n_flights=30))
        session.add(RefSchedule(callsign="EZY8125", org="LGW", dst="ACE",
                                airline_icao="EZY", dep_min=1056, arr_min=1320,
                                type_code=None, n_flights=30))
        session.commit()
        refdata_ingest.ingest_boards(session, str(bdb))
        session.commit()
        named = session.get(RefSchedule, ("EZY8124", "LGW", "ACE"))
        other = session.get(RefSchedule, ("EZY8125", "LGW", "ACE"))
        assert named.flight == "EZY8124" and named.source == "both"
        assert named.dep_min == 1055 and other.flight is None
        assert session.get(RefSchedule, ("EZY8124", "LGW", None)) is None
    finally:
        session.close()


def test_a_board_number_pins_its_regional_operator(ctx, tmp_path):
    import sqlite3 as s3
    from app import refdata_ingest
    from app.refdata_models import RefAirline, RefSchedule
    client, app, sm, settings, readsb = ctx
    bdb = tmp_path / "boards.db"
    conn = s3.connect(str(bdb))
    conn.execute("CREATE TABLE boards (airport TEXT, kind TEXT, flight TEXT,"
                 " counterpart TEXT, sched_min INT, day TEXT, source TEXT,"
                 " fetched_at TEXT)")
    conn.execute("INSERT INTO boards VALUES ('CAK','dep','AA5062','DCA',332,"
                 "'2026-09-23','t','now')")
    conn.execute("INSERT INTO boards VALUES ('CAK','dep','UA4646','ORD',360,"
                 "'2026-09-23','t','now')")
    conn.commit(); conn.close()
    session = sm()
    try:
        session.add(RefAirline(icao="AAL", iata="AA", name="American", palette=[]))
        session.add(RefAirline(icao="UAL", iata="UA", name="United", palette=[]))
        # PSA flies American's number; a service with no airline known
        # flies United's, same digits
        session.add(RefSchedule(callsign="JIA5062", org="CAK", dst="DCA",
                                airline_icao="JIA", dep_min=330, arr_min=400,
                                type_code=None, n_flights=30))
        session.add(RefSchedule(callsign="GJS4646", org="CAK", dst="ORD",
                                airline_icao="", dep_min=362, arr_min=420,
                                type_code=None, n_flights=20))
        session.commit()
        refdata_ingest.ingest_boards(session, str(bdb))
        session.commit()
        psa = session.get(RefSchedule, ("JIA5062", "CAK", "DCA"))
        unk = session.get(RefSchedule, ("GJS4646", "CAK", "ORD"))
        assert psa.flight == "AA5062" and psa.source == "both"
        assert unk.flight == "UA4646" and unk.source == "both"
    finally:
        session.close()


def test_a_marketed_number_resolves_to_its_callsign(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    db = tmp_path / "legs.db"
    _build(str(db), AIRPORT_LEGS)
    app.state.legs = LegBook(str(db))
    app.state.routes = RouteBook(str(tmp_path / "absent.json.gz"))
    from app.refdata_models import RefSchedule
    with sm() as session:
        session.add(RefSchedule(callsign="SQ322", org="SIN", dst="LHR",
                                airline_icao="SQ3", dep_min=16 * 60,
                                arr_min=22 * 60 + 35, type_code="A359",
                                n_flights=30, flight="SQ22", source="both"))
        session.commit()
    body = client.get("/v1/flights/sq22").json()
    assert body["callsign"] == "SQ322" and body["marketed"] == "SQ22"
    assert body["legs"][0]["flight"] == "SQ22" and body["legs"][0]["times"] == "both"


def test_a_published_only_service_answers_from_the_schedule(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    app.state.legs = LegBook(str(tmp_path / "absent.db"))
    app.state.routes = RouteBook(str(tmp_path / "absent.json.gz"))
    from app.refdata_models import RefSchedule
    with sm() as session:
        session.add(RefSchedule(callsign="LH996", org="FRA", dst="AMS",
                                airline_icao="DLH", dep_min=16 * 60 + 30,
                                arr_min=17 * 60 + 40, type_code=None,
                                n_flights=1, flight="LH996", source="published"))
        session.commit()
    body = client.get("/v1/flights/LH996").json()
    assert body["route"] == ["FRA", "AMS"] and body["route_source"] == "published"
    assert body["coverage"] == "published"
    leg = body["legs"][0]
    assert leg["dep"] == "16:30" and leg["arr"] == "17:40" and leg["times"] == "published"


def test_an_arrivals_board_names_the_service_too(ctx, tmp_path):
    import sqlite3 as s3
    from app import refdata_ingest
    from app.refdata_models import RefAirline, RefSchedule
    client, app, sm, settings, readsb = ctx
    bdb = tmp_path / "boards.db"
    conn = s3.connect(str(bdb))
    conn.execute("CREATE TABLE boards (airport TEXT, kind TEXT, flight TEXT,"
                 " counterpart TEXT, sched_min INT, day TEXT, source TEXT,"
                 " fetched_at TEXT)")
    conn.execute("INSERT INTO boards VALUES ('LHR','arr','BA272','SAN',860,"
                 "'2026-09-22','t','now')")
    conn.execute("INSERT INTO boards VALUES ('LHR','arr','VS9','JFK',600,"
                 "'2026-09-22','t','now')")
    conn.commit(); conn.close()
    session = sm()
    try:
        session.add(RefAirline(icao="BAW", iata="BA", name="British Airways", palette=[]))
        session.add(RefAirline(icao="VIR", iata="VS", name="Virgin Atlantic", palette=[]))
        session.add(RefSchedule(callsign="BAW9SW", org="SAN", dst="LHR",
                                airline_icao="BAW", dep_min=1195, arr_min=815,
                                type_code=None, n_flights=29))
        session.commit()
        refdata_ingest.ingest_boards(session, str(bdb))
        session.commit()
        ba = session.get(RefSchedule, ("BAW9SW", "SAN", "LHR"))
        assert ba.flight == "BA272" and ba.source == "both" and ba.arr_min == 860
        vs = session.get(RefSchedule, ("VS9", "JFK", "LHR"))
        assert vs is not None and vs.arr_min == 600 and vs.source == "published"
    finally:
        session.close()
