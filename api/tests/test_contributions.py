"""Gaps published, submissions filed as claims, verdicts from evidence,
the catalog served after approval."""
import datetime
import gzip
import json

from app import contributions, refdata_ingest
from app.gaps_db import GapBook
from app.legs_db import LegBook
from app.refdata_models import Claim, Endorsement, RefAirport, RouteCatalog
from app.routes_db import RouteBook

AIRPORTS_CSV = (
    '"id","ident","type","name","latitude_deg","longitude_deg",'
    '"elevation_ft","continent","iso_country","iso_region","municipality",'
    '"scheduled_service","icao_code","iata_code","gps_code","local_code",'
    '"home_link","wikipedia_link","keywords"\n'
    '1,"WSSS","large_airport","Singapore Changi",1.35019,103.994,22,"AS",'
    '"SG","SG-04","Singapore","yes","WSSS","SIN","WSSS",,,,\n'
    '2,"ZUTF","large_airport","Chengdu Tianfu International Airport",'
    '30.3125,104.4415,1500,"AS","CN","CN-51","Chengdu","yes","ZUTF","TFU",'
    '"ZUTF",,,,\n'
    '3,"YPPH","large_airport","Perth International Airport",-31.94,115.967,'
    '67,"OC","AU","AU-WA","Perth","yes","YPPH","PER","YPPH",,,,\n'
    '4,"WSAC","medium_airport","Changi Air Base (East)",1.344,104.009,22,'
    '"AS","SG","SG-04","Singapore","no","WSAC",,"WSAC",,,,\n'
    '5,"EGLL","large_airport","London Heathrow",51.47,-0.46,83,"EU","GB",'
    '"GB-ENG","London","yes","EGLL","LHR","EGLL",,,,\n'
    '6,"RJAA","large_airport","Narita",35.76,140.39,141,"AS","JP","JP-12",'
    '"Narita","yes","RJAA","NRT","RJAA",,,,\n'
    '7,"SBGR","large_airport","Guarulhos",-23.43,-46.47,2461,"SA","BR",'
    '"BR-SP","São Paulo","yes","SBGR","GRU","SBGR",,,,\n'
    '8,"SAEZ","large_airport","Ezeiza",-34.82,-58.54,67,"SA","AR","AR-B",'
    '"Buenos Aires","yes","SAEZ","EZE","SAEZ",,,,\n'
    '9,"HAAB","large_airport","Bole",8.98,38.8,7625,"AF","ET","ET-AA",'
    '"Addis Ababa","yes","HAAB","ADD","HAAB",,,,\n'
    '10,"ZUUU","large_airport","Chengdu Shuangliu",30.58,103.95,1625,"AS",'
    '"CN","CN-51","Chengdu","yes","ZUUU","CTU","ZUUU",,,,\n'
    '11,"ZBAA","large_airport","Beijing Capital",40.08,116.58,116,"AS","CN",'
    '"CN-11","Beijing","yes","ZBAA","PEK","ZBAA",,,,\n'
)

# SIA842 leaves SIN daily and is last heard over Vietnam heading north,
# nine hours from its return; SIA843 is the mirror with a rare glimpse of
# Tianfu. QFA9 carries nothing but a known end.
GAPS = {
    "SIA842": {"side": "dest", "known": "SIN", "hint": None, "type": "B78X",
               "n_recent": 13, "last_seen": "2026-09-06",
               "last_lat": 12.41, "last_lon": 106.92, "last_trk": 21,
               "est_km": 3150, "n_rot": 5},
    "SIA843": {"side": "origin", "known": "SIN", "hint": "TFU",
               "type": "B78X", "n_recent": 12, "last_seen": "2026-09-06",
               "last_lat": None, "last_lon": None, "last_trk": None,
               "est_km": None, "n_rot": 0},
    "QFA9": {"side": "dest", "known": "PER", "hint": None, "type": None,
             "n_recent": 3, "last_seen": "2026-09-01",
             "last_lat": None, "last_lon": None, "last_trk": None,
             "est_km": None, "n_rot": 0},
    "CFSUG": {"side": "dest", "known": "SIN", "hint": None, "type": None,
              "n_recent": 420, "last_seen": "2026-09-07"},
    # ADD->GRU->EZE with Addis unseen: the origin is asked, the chain known.
    "ETH506": {"side": "origin", "known": "GRU", "hint": None,
               "chain": ["GRU", "EZE"], "type": "A359", "n_recent": 28,
               "last_seen": "2026-09-06", "last_lat": None, "last_lon": None,
               "last_trk": None, "est_km": None, "n_rot": 0},
}


def _write_gz(path, data):
    with gzip.open(path, "wt") as fh:
        json.dump(data, fh)


ROUTES = {"SIA800": ["SIN", "PEK"], "SIA802": ["SIN", "PEK"],
          "SIA850": ["SIN", "TFU"], "SIA632": ["SIN", "NRT"],
          "SIA224": ["SIN", "PER"], "QFA1": ["SYD", "SIN", "LHR"]}


def _setup(ctx, tmp_path, routes=None):
    client, app, sm, settings, readsb = ctx
    airports = tmp_path / "airports.csv"
    airports.write_text(AIRPORTS_CSV)
    session = sm()
    try:
        refdata_ingest.ingest_airports(session, str(airports))
        session.commit()
    finally:
        session.close()
    _write_gz(tmp_path / "gaps.json.gz", GAPS)
    _write_gz(tmp_path / "routes.json.gz", routes if routes is not None else ROUTES)
    app.state.gaps = GapBook(str(tmp_path / "gaps.json.gz"))
    app.state.routes = RouteBook(str(tmp_path / "routes.json.gz"))
    app.state.legs = LegBook(str(tmp_path / "absent.db"))
    return client, app, sm


def _sub(i, callsign, **fields):
    return dict({"id": i, "callsign": callsign,
                 "received_at": "2026-09-08T10:00:00Z"}, **fields)


def _pull(sm, app, subs):
    session = sm()
    try:
        pages = [subs]
        return contributions.pull(session, app.state.gaps,
                                  lambda after: [s for s in pages.pop(0)
                                                 if s["id"] > after]
                                  if pages else [], routes=app.state.routes)
    finally:
        session.close()


def test_airport_roles_from_the_registry(ctx, tmp_path):
    _, _, sm = _setup(ctx, tmp_path)
    session = sm()
    try:
        roles = {a.ident: a.role for a in session.query(RefAirport)}
    finally:
        session.close()
    assert roles["WSSS"] == "commercial" and roles["WSAC"] == "military"
    assert refdata_ingest.airport_role("closed", "no", "Anything") == "closed"
    assert refdata_ingest.airport_role("small_airport", "no",
                                       "Old Field") == "general"
    assert refdata_ingest.airport_role("medium_airport", "no",
                                       "RAF Brize Norton") == "military"


def test_gaps_listed_most_seen_first(ctx, tmp_path):
    client, _, _ = _setup(ctx, tmp_path)
    body = client.get("/v1/gaps").json()
    assert body["total"] == 4                       # CFSUG is not a flight
    assert [g["callsign"] for g in body["gaps"]] == ["ETH506", "SIA842",
                                                     "SIA843", "QFA9"]
    assert body["gaps"][0]["chain"] == ["GRU", "EZE"]
    first = body["gaps"][1]
    assert first["last_heard"] == {"lat": 12.41, "lon": 106.92, "track": 21}
    assert first["rotation_km"] == 3150 and first["type"] == "B78X"
    assert body["gaps"][3]["last_heard"] is None
    assert client.get("/v1/gaps?airline=QFA").json()["total"] == 1
    one = client.get("/v1/gaps/sia842").json()
    assert one["catalog"] is None and one["answers"] == []
    assert client.get("/v1/gaps/SIA321").status_code == 404
    assert client.get("/v1/gaps/CFSUG").status_code == 404


def test_gaps_dark_without_artifact(ctx, tmp_path):
    client, app, _ = _setup(ctx, tmp_path)
    app.state.gaps = GapBook(str(tmp_path / "absent.json.gz"))
    assert client.get("/v1/gaps").status_code == 503


def test_read_api_takes_no_writes(ctx, tmp_path):
    client, _, _ = _setup(ctx, tmp_path)
    assert client.post("/v1/contributions", json={}).status_code == 404
    assert client.post("/v1/gaps", json={}).status_code == 405


def test_corroborated_claim_approves_itself(ctx, tmp_path):
    """Tianfu lies along the last track and at the rotation's distance:
    two signals, no failure, the catalog gains the route without anyone
    reviewing it, and the flight page says where it came from."""
    client, app, sm = _setup(ctx, tmp_path)
    counts = _pull(sm, app, [_sub(1, "SIA842", dest="ZUTF",
                                  handle="spotter sg", note="daily")])
    assert counts["filed"] == 1 and counts["approved"] == 1
    session = sm()
    try:
        claim = session.query(Claim).one()
        assert (claim.origin, claim.dest, claim.status) == ("SIN", "TFU",
                                                            "approved")
        assert claim.verdict == "corroborated"
        assert claim.reviewed_by == "verdict"
        checks = claim.checks
        assert checks["corridor"] == "pass" and checks["rotation"] == "pass"
        assert checks["type"] == "pass" and checks["mirror"] == "pass"
        assert checks["network"] == "pass" and checks["unique"] == "pass"
        assert checks["named"] == 1 and checks["keyed"] == 0
        assert contributions.catalog_current(session, "SIA842").dest == "TFU"
    finally:
        session.close()
    body = client.get("/v1/flights/SIA842").json()
    assert body["route"] == ["SIN", "TFU"]
    assert body["route_source"] == "observed+catalog"
    one = client.get("/v1/gaps/SIA842").json()
    assert one["catalog"]["route"] == ["SIN", "TFU"]
    assert one["answers"] == [{"origin": "SIN", "dest": "TFU",
                               "status": "approved",
                               "verdict": "corroborated"}]
    assert client.get("/v1/contributors").json()["contributors"] == [
        {"handle": "spotter sg", "answers": 1,
         "latest": str(datetime.date.today())}]


def test_contradicted_claim_rejects_itself(ctx, tmp_path):
    """London is behind the aircraft and three times the rotation's
    distance."""
    client, app, sm = _setup(ctx, tmp_path)
    counts = _pull(sm, app, [_sub(1, "SIA842", dest="LHR", handle="x")])
    assert counts["rejected"] == 1
    session = sm()
    try:
        claim = session.query(Claim).one()
        assert (claim.status, claim.verdict) == ("rejected", "contradicted")
        assert claim.checks["corridor"] == "fail"
        assert claim.checks["rotation"] == "fail"
        assert session.query(RouteCatalog).count() == 0
    finally:
        session.close()
    assert client.get("/v1/flights/SIA842").status_code == 404


def test_unverified_claim_waits_for_the_operator(ctx, tmp_path):
    """QFA9 has a known end and nothing else: no signal either way."""
    client, app, sm = _setup(ctx, tmp_path)
    counts = _pull(sm, app, [_sub(1, "QFA9", dest="SIN")])
    assert counts["pending"] == 1 and counts["approved"] == 0
    session = sm()
    try:
        claim = session.query(Claim).one()
        assert (claim.status, claim.verdict) == ("pending", "unverified")
        assert claim.anonymous_count == 1
        assert session.query(Endorsement).count() == 0
        contributions.approve(session, app.state.gaps, claim.id,
                              note="checked the timetable")
        assert contributions.catalog_current(session, "QFA9").dest == "SIN"
    finally:
        session.close()
    assert client.get("/v1/flights/QFA9").json()["route_source"] == \
        "observed+catalog"


def test_two_keys_count_as_a_signal(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    subs = [_sub(1, "QFA9", dest="SIN", key_name="alice"),
            _sub(2, "QFA9", dest="SIN", key_name="bob"),
            _sub(3, "QFA9", dest="SIN"),
            _sub(4, "QFA9", dest="WSSS", handle="carol")]
    counts = _pull(sm, app, subs)
    assert counts["filed"] == 4
    session = sm()
    try:
        claim = session.query(Claim).one()          # one claim, four voices
        assert claim.anonymous_count == 1
        assert claim.checks["keyed"] == 2 and claim.checks["named"] == 1
        assert claim.verdict == "unverified"        # one signal is not enough
        assert claim.status == "pending"
    finally:
        session.close()


def test_mirror_and_hint_disagreement_contradicts(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    counts = _pull(sm, app, [_sub(1, "SIA843", origin="PER", handle="x")])
    assert counts["rejected"] == 1
    session = sm()
    try:
        assert session.query(Claim).one().checks["observation"] == "fail"
    finally:
        session.close()


def test_questions_cap_and_junk_is_dropped(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    subs = [_sub(1, "SIA321", dest="LHR"),          # no open question
            _sub(2, "QFA9", dest="ZZZZ"),           # unknown airport
            _sub(3, "CFSUG", dest="LHR"),           # not a flight number
            _sub(4, "QFA9", dest="SIN"),
            _sub(5, "QFA9", dest="LHR"),
            _sub(6, "QFA9", dest="NRT"),
            _sub(7, "QFA9", dest="TFU")]            # fourth distinct answer
    counts = _pull(sm, app, subs)
    assert counts["dropped"] == 4 and counts["filed"] == 3
    session = sm()
    try:
        claims = session.query(Claim).order_by(Claim.id).all()
        assert [c.dest for c in claims] == ["SIN", "LHR", "NRT"]
        assert all(c.verdict == "contested" for c in claims)
        assert all(c.status == "pending" for c in claims)
    finally:
        session.close()


def test_pull_resumes_from_its_cursor(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    _pull(sm, app, [_sub(1, "QFA9", dest="SIN"), _sub(2, "QFA9", dest="SIN")])
    counts = _pull(sm, app, [_sub(1, "QFA9", dest="SIN"),
                             _sub(2, "QFA9", dest="SIN"),
                             _sub(3, "QFA9", dest="SIN", handle="dee")])
    assert counts["received"] == 1
    session = sm()
    try:
        claim = session.query(Claim).one()
        assert claim.anonymous_count == 2
        assert session.query(Endorsement).one().handle == "dee"
    finally:
        session.close()


def test_observation_outranks_the_catalog(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    _pull(sm, app, [_sub(1, "SIA842", dest="TFU", handle="x")])
    _write_gz(tmp_path / "routes2.json.gz", {"SIA842": ["SIN", "PER"]})
    app.state.routes = RouteBook(str(tmp_path / "routes2.json.gz"))
    body = client.get("/v1/flights/SIA842").json()
    assert body["route"] == ["SIN", "PER"]
    assert body["route_source"] == "observed"
    session = sm()
    try:
        closed = contributions.reconcile(session, app.state.routes)
        assert closed == [("SIA842", "contradicted")]
        assert contributions.catalog_current(session, "SIA842") is None
    finally:
        session.close()


def test_supersession_is_dated(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    _pull(sm, app, [_sub(1, "QFA9", dest="SIN")])
    _pull(sm, app, [_sub(2, "QFA9", dest="NRT", valid_from="2026-10-01",
                         handle="x")])
    session = sm()
    try:
        first, second = session.query(Claim).order_by(Claim.id).all()
        contributions.approve(session, app.state.gaps, first.id)
        contributions.approve(session, app.state.gaps, second.id)
        rows = session.query(RouteCatalog).order_by(RouteCatalog.id).all()
        assert rows[0].valid_to == datetime.date(2026, 10, 1)
        assert rows[0].closed_reason == "superseded"
        assert rows[1].valid_from == datetime.date(2026, 10, 1)
        assert contributions.catalog_current(
            session, "QFA9", datetime.date(2026, 9, 20)).dest == "SIN"
        assert contributions.catalog_current(
            session, "QFA9", datetime.date(2026, 10, 2)).dest == "NRT"
    finally:
        session.close()


def test_rejected_claims_are_purged_after_ninety_days(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    _pull(sm, app, [_sub(1, "SIA842", dest="LHR", handle="x")])
    session = sm()
    try:
        claim = session.query(Claim).one()
        claim.reviewed_at = datetime.datetime.now(datetime.timezone.utc) \
            - datetime.timedelta(days=91)
        session.commit()
        counts = contributions.pull(session, app.state.gaps,
                                    lambda after: [])
        assert counts["purged"] == 1
        assert session.query(Claim).count() == 0
        assert session.query(Endorsement).count() == 0
    finally:
        session.close()


def test_open_chain_answer_serves_the_whole_route(ctx, tmp_path):
    """ETH506 asked for its origin; the answer ADD makes the catalog
    route ADD->GRU->EZE, stops included, and the flight page says so."""
    client, app, sm = _setup(ctx, tmp_path)
    counts = _pull(sm, app, [_sub(1, "ETH506", origin="ADD", handle="x")])
    assert counts["pending"] == 1
    session = sm()
    try:
        claim = session.query(Claim).one()
        assert (claim.origin, claim.dest) == ("ADD", "GRU")
        assert claim.checks["known_end"] == "pass"
        contributions.approve(session, app.state.gaps, claim.id)
        current = contributions.catalog_current(session, "ETH506")
        assert contributions.catalog_route(current) == ["ADD", "GRU", "EZE"]
    finally:
        session.close()
    body = client.get("/v1/flights/ETH506").json()
    assert body["route"] == ["ADD", "GRU", "EZE"]
    assert body["route_source"] == "observed+catalog"
    assert client.get("/v1/gaps/ETH506").json()["catalog"]["route"] == \
        ["ADD", "GRU", "EZE"]


def test_suggestions_follow_the_evidence(ctx, tmp_path):
    """SIA842 from SIN, 3,150 km, heading north-north-east, on a 787:
    the two Chengdu airports fit, Tianfu first because the airline is
    seen there; Beijing is too far, Perth is behind the aircraft."""
    client, _, _ = _setup(ctx, tmp_path)
    body = client.get("/v1/gaps?airline=SIA").json()
    by = {g["callsign"]: g for g in body["gaps"]}
    assert by["SIA842"]["suggested"] == ["TFU", "CTU"]
    assert by["SIA843"]["suggested"] == []           # no rotation yet
    assert client.get("/v1/gaps/SIA842").json()["suggested"] == ["TFU", "CTU"]


def test_propose_files_the_sole_airport_the_airline_flies_to(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    session = sm()
    try:
        filed, approved = contributions.propose(session, app.state.gaps,
                                                app.state.routes)
        assert (filed, approved) == (1, 1)
        claim = session.query(Claim).one()
        assert (claim.callsign, claim.dest, claim.status) == \
            ("SIA842", "TFU", "approved")
        assert claim.reviewed_by == "verdict"
        assert session.query(Endorsement).one().key_name == "evidence"
        # a second pass leaves it alone
        assert contributions.propose(session, app.state.gaps,
                                     app.state.routes) == (0, 0)
    finally:
        session.close()
    assert client.get("/v1/flights/SIA842").json()["route"] == ["SIN", "TFU"]
    assert client.get("/v1/contributors").json()["contributors"] == []


def test_answered_questions_leave_the_list_and_can_be_withdrawn(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    session = sm()
    try:
        contributions.propose(session, app.state.gaps, app.state.routes)
        assert client.get("/v1/gaps").json()["total"] == 3       # SIA842 answered
        assert "SIA842" not in [g["callsign"] for g in
                                client.get("/v1/gaps").json()["gaps"]]
        claim = session.query(Claim).one()
        contributions.withdraw(session, claim.id, note="looked wrong")
        assert claim.status == "rejected"
        row = session.query(RouteCatalog).one()
        assert row.closed_reason == "withdrawn" and row.valid_to is not None
    finally:
        session.close()
    assert client.get("/v1/gaps").json()["total"] == 4
    assert client.get("/v1/flights/SIA842").status_code == 404
