"""Gaps published, answers checked, catalog served after approval."""
import datetime
import gzip
import json

from app import contributions, refdata_ingest
from app.gaps_db import GapBook
from app.legs_db import LegBook
from app.refdata_models import Contribution, RefAirport, RouteCatalog
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
    '5,"XXXX","small_airport","Old Field","0.5","0.5",0,"AF","ZZ","ZZ-1",'
    '"Nowhere","no",,,,,,,\n'
)

# SIA842 as observed: leaves SIN daily, last heard over Vietnam heading
# north-north-east. SIA843 is the mirror with a rare glimpse of Tianfu.
GAPS = {
    "SIA842": {"side": "dest", "known": "SIN", "hint": None,
               "n_recent": 13, "last_seen": "2026-09-06",
               "last_lat": 12.41, "last_lon": 106.92, "last_trk": 21},
    "SIA843": {"side": "origin", "known": "SIN", "hint": "TFU",
               "n_recent": 12, "last_seen": "2026-09-06",
               "last_lat": 1.36, "last_lon": 103.99, "last_trk": None},
    "QFA9": {"side": "dest", "known": "PER", "hint": None,
             "n_recent": 3, "last_seen": "2026-09-01",
             "last_lat": None, "last_lon": None, "last_trk": None},
    # A registration flying as its own callsign is not a question.
    "CFSUG": {"side": "dest", "known": "YEG", "hint": None,
              "n_recent": 420, "last_seen": "2026-09-07",
              "last_lat": None, "last_lon": None, "last_trk": None},
}


def _write_gz(path, data):
    with gzip.open(path, "wt") as fh:
        json.dump(data, fh)


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
    _write_gz(tmp_path / "routes.json.gz", routes or {})
    app.state.gaps = GapBook(str(tmp_path / "gaps.json.gz"))
    app.state.routes = RouteBook(str(tmp_path / "routes.json.gz"))
    app.state.legs = LegBook(str(tmp_path / "absent.db"))
    return client, app, sm


def test_airport_roles_from_the_registry(ctx, tmp_path):
    _, _, sm = _setup(ctx, tmp_path)
    session = sm()
    try:
        roles = {a.ident: a.role for a in session.query(RefAirport)}
    finally:
        session.close()
    assert roles == {"WSSS": "commercial", "ZUTF": "commercial",
                     "YPPH": "commercial", "WSAC": "military",
                     "XXXX": "general"}
    assert refdata_ingest.airport_role("closed", "no", "Anything") == "closed"
    assert refdata_ingest.airport_role("medium_airport", "no",
                                       "RAF Brize Norton") == "military"


def test_gaps_listed_most_seen_first(ctx, tmp_path):
    client, _, _ = _setup(ctx, tmp_path)
    body = client.get("/v1/gaps").json()
    assert body["total"] == 3
    assert [g["callsign"] for g in body["gaps"]] == ["SIA842", "SIA843",
                                                     "QFA9"]
    first = body["gaps"][0]
    assert first["side"] == "dest" and first["known"] == "SIN"
    assert first["last_heard"] == {"lat": 12.41, "lon": 106.92, "track": 21}
    assert body["gaps"][2]["last_heard"] is None
    assert client.get("/v1/gaps?airline=QFA").json()["total"] == 1
    assert client.get("/v1/gaps?side=origin").json()["total"] == 1
    assert client.get("/v1/gaps/sia842").json()["catalog"] is None
    assert client.get("/v1/gaps/SIA321").status_code == 404


def test_gaps_dark_without_artifact(ctx, tmp_path):
    client, app, _ = _setup(ctx, tmp_path)
    app.state.gaps = GapBook(str(tmp_path / "absent.json.gz"))
    assert client.get("/v1/gaps").status_code == 503
    assert client.post("/v1/contributions",
                       json={"callsign": "SIA842", "dest": "TFU"}
                       ).status_code == 503


def test_answer_is_checked_against_observation(ctx, tmp_path):
    client, _, sm = _setup(ctx, tmp_path)
    response = client.post("/v1/contributions",
                           json={"callsign": "SIA842", "dest": "ZUTF",
                                 "note": "daily to Chengdu Tianfu"})
    assert response.status_code == 202
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["status"], body["origin"], body["dest"]) == \
        ("pending", "SIN", "TFU")          # known end filled, code canonical
    assert body["checks"] == {"known_end": "pass", "not_same": "pass",
                              "airport": "pass", "observation": "skip",
                              "corridor": "pass", "agreeing": 0}
    # Perth is behind the aircraft: the corridor check says so, the
    # answer is still recorded for the reviewer, and nothing is served.
    off = client.post("/v1/contributions",
                      json={"callsign": "SIA842", "origin": "SIN",
                            "dest": "PER"}).json()
    assert off["checks"]["corridor"] == "fail"
    # A second identical answer counts the first as agreeing.
    again = client.post("/v1/contributions",
                        json={"callsign": "SIA842", "dest": "TFU"}).json()
    assert again["checks"]["agreeing"] == 1
    # The mirror question carries a rare observation of the missing end.
    mirror = client.post("/v1/contributions",
                         json={"callsign": "SIA843", "origin": "TFU"}).json()
    assert mirror["checks"]["observation"] == "pass"
    assert mirror["checks"]["corridor"] == "skip"
    wrong = client.post("/v1/contributions",
                        json={"callsign": "SIA843", "origin": "PER"}).json()
    assert wrong["checks"]["observation"] == "fail"
    session = sm()
    try:
        assert session.query(Contribution).count() == 5
        assert session.query(RouteCatalog).count() == 0
    finally:
        session.close()
    assert client.get("/v1/flights/SIA842").status_code == 404


def test_answers_outside_the_questions_are_refused(ctx, tmp_path):
    client, _, sm = _setup(ctx, tmp_path)
    r = client.post("/v1/contributions", json={"callsign": "SIA321",
                                               "dest": "LHR"})
    assert r.status_code == 422
    assert r.json()["detail"] == "no open question for this callsign"
    r = client.post("/v1/contributions", json={"callsign": "SIA842",
                                               "dest": "WSAC"})
    assert r.status_code == 422           # an air base is not an answer
    r = client.post("/v1/contributions", json={"callsign": "SIA842",
                                               "dest": "ZZZZ"})
    assert r.status_code == 422
    r = client.post("/v1/contributions", json={"callsign": "SIA842"})
    assert r.status_code == 422
    r = client.post("/v1/contributions", json={"callsign": "SIA842",
                                               "dest": "T-FU"})
    assert r.status_code == 422
    session = sm()
    try:
        assert session.query(Contribution).count() == 0
    finally:
        session.close()


def test_approval_serves_the_route_with_its_provenance(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    cid = client.post("/v1/contributions",
                      json={"callsign": "SIA842", "dest": "TFU"}).json()["id"]
    session = sm()
    try:
        contributions.approve(session, cid, note="matches the timetable")
        row = session.get(Contribution, cid)
        assert row.status == "approved" and row.review_note
        current = contributions.catalog_current(session, "SIA842")
        assert (current.origin, current.dest, current.valid_to) == \
            ("SIN", "TFU", None)
        assert current.contribution_id == cid
    finally:
        session.close()
    body = client.get("/v1/flights/SIA842").json()
    assert body["route"] == ["SIN", "TFU"]
    assert body["route_source"] == "observed+catalog"
    assert client.get("/v1/gaps/SIA842").json()["catalog"] == {
        "origin": "SIN", "dest": "TFU",
        "valid_from": str(datetime.date.today())}

    # A later answer supersedes: the old row closes on the new start date.
    cid2 = client.post("/v1/contributions",
                       json={"callsign": "SIA842", "dest": "PER",
                             "valid_from": "2026-10-01"}).json()["id"]
    session = sm()
    try:
        contributions.approve(session, cid2)
        rows = session.query(RouteCatalog).order_by(RouteCatalog.id).all()
        assert rows[0].valid_to == datetime.date(2026, 10, 1)
        assert rows[0].closed_reason == "superseded"
        assert contributions.catalog_current(
            session, "SIA842", datetime.date(2026, 9, 20)).dest == "TFU"
        assert contributions.catalog_current(
            session, "SIA842", datetime.date(2026, 10, 2)).dest == "PER"
    finally:
        session.close()


def test_observation_outranks_the_catalog(ctx, tmp_path):
    client, app, sm = _setup(ctx, tmp_path)
    cid = client.post("/v1/contributions",
                      json={"callsign": "SIA842", "dest": "TFU"}).json()["id"]
    session = sm()
    try:
        contributions.approve(session, cid)
    finally:
        session.close()
    # Coverage reaches Chengdu: the artifact now carries the full route.
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


def test_rejection_keeps_the_record_and_serves_nothing(ctx, tmp_path):
    client, _, sm = _setup(ctx, tmp_path)
    cid = client.post("/v1/contributions",
                      json={"callsign": "QFA9", "dest": "SIN"}).json()["id"]
    session = sm()
    try:
        contributions.reject(session, cid, note="no such service")
        row = session.get(Contribution, cid)
        assert row.status == "rejected"
        assert session.query(RouteCatalog).count() == 0
    finally:
        session.close()
    assert client.get("/v1/flights/QFA9").status_code == 404
