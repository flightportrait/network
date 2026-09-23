"""The observed airframe history import: stable ids across nightly
re-imports, spells and events replaced wholesale, a truncated artifact
refused."""
import gzip
import json

import pytest
from sqlalchemy import select

from app import airframe_history
from app.refdata_models import Airframe, AirframeEvent, AirframeSpell

DOC = {
    "evidence_from": "2025-08-28", "evidence_to": "2026-09-21",
    "airframes": {
        "49d283": {"first": "2025-08-28", "last": "2026-09-21", "n": 865,
                   "type": "B738",
                   "regs": [["OK-TVY", "2025-08-28", "2026-09-21", 865]],
                   "ops": [["TVS", "2025-09-10", "2025-11-29", 128],
                           ["KNE", "2025-12-17", "2026-03-28", 38],
                           ["TVS", "2026-05-08", "2026-09-21", 583]],
                   "gaps": [["2026-03-28", "2026-05-08"]]},
        "4b1a2b": {"first": "2026-06-02", "last": "2026-09-20", "n": 120,
                   "type": "A20N",
                   "regs": [["HB-JDA", "2026-06-02", "2026-07-01", 20],
                            ["HB-JDB", "2026-07-03", "2026-09-20", 100]],
                   "ops": [["SWR", "2026-06-02", "2026-09-20", 120]],
                   "gaps": []},
    },
}


def _write(tmp_path, doc, name="h.json.gz"):
    path = tmp_path / name
    path.write_bytes(gzip.compress(json.dumps(doc).encode()))
    return str(path)


def _events(session):
    return sorted((e.kind, e.at.date().isoformat(), json.dumps(e.detail))
                  for e in session.execute(select(AirframeEvent)).scalars())


def test_history_import_and_reimport(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    path = _write(tmp_path, DOC)
    with sm() as session:
        assert airframe_history.ingest_history(session, path) == 2
        session.commit()
        ids = {s.value: s.airframe_id for s in session.execute(
            select(AirframeSpell).where(AirframeSpell.kind == "hex")).scalars()}
        first = _events(session)
    assert ("operator_change", "2025-12-17",
            json.dumps({"from": "TVS", "to": "KNE"})) in first
    assert ("operator_change", "2026-05-08",
            json.dumps({"from": "KNE", "to": "TVS"})) in first
    assert ("registration_change", "2026-07-03",
            json.dumps({"from": "HB-JDA", "to": "HB-JDB"})) in first
    assert ("not_observed", "2026-03-29", json.dumps(
        {"last_seen": "2026-03-28", "seen_again": "2026-05-08",
         "days": 40})) in first
    # first sighting inside the archive's first month is not an event
    kinds = {(k, d) for k, d, _ in first}
    assert ("first_observed", "2026-06-02") in kinds
    assert not any(k == "first_observed" and d == "2025-08-28"
                   for k, d in kinds)

    # a second night: same ids, no duplicated spells or events
    with sm() as session:
        airframe_history.ingest_history(session, path)
        session.commit()
        again = {s.value: s.airframe_id for s in session.execute(
            select(AirframeSpell).where(AirframeSpell.kind == "hex")).scalars()}
        assert again == ids
        assert _events(session) == first
        assert session.query(Airframe).count() == 2
        assert session.query(AirframeSpell).count() == 2 + 1 + 3 + 2 + 1
        frame = session.get(Airframe, ids["49d283"])
        assert frame.type_code == "B738"
        assert frame.last_observed.isoformat() == "2026-09-21"


def test_history_leaves_other_sources_alone(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    path = _write(tmp_path, DOC)
    with sm() as session:
        airframe_history.ingest_history(session, path)
        session.commit()
        frame_id = session.execute(select(AirframeSpell.airframe_id)
                                   .where(AirframeSpell.value == "49d283")
                                   ).scalar_one()
        import datetime
        session.add(AirframeEvent(
            airframe_id=frame_id, kind="squawk", source="live",
            at=datetime.datetime(2026, 9, 1, 12, tzinfo=datetime.timezone.utc),
            detail={"code": "7700"}))
        session.commit()
        airframe_history.ingest_history(session, path)
        session.commit()
        assert session.execute(select(AirframeEvent.kind)
                               .where(AirframeEvent.source == "live")
                               ).scalar_one() == "squawk"


def test_truncated_artifact_is_refused(ctx, tmp_path, monkeypatch):
    client, app, sm, settings, readsb = ctx
    monkeypatch.setattr(airframe_history, "GUARD_FLOOR", 2)
    with sm() as session:
        airframe_history.ingest_history(session, _write(tmp_path, DOC))
        session.commit()
    short = dict(DOC, airframes={"49d283": DOC["airframes"]["49d283"]})
    with sm() as session:
        with pytest.raises(ValueError):
            airframe_history.ingest_history(
                session, _write(tmp_path, short, "short.json.gz"))
        session.rollback()
        assert session.query(AirframeSpell).count() == 9


def test_airframe_endpoint_serves_the_history(ctx, tmp_path):
    client, app, sm, settings, readsb = ctx
    from app.refdata_models import RefAirframe, RefAirline
    import datetime
    with sm() as session:
        airframe_history.ingest_history(session, _write(tmp_path, DOC))
        session.add(RefAirframe(hex="49d283", registration="OK-TVY",
                                type_code="B738", source="tar1090"))
        session.add(RefAirline(icao="KNE", name="flynas", palette=[]))
        frame_id = session.execute(select(AirframeSpell.airframe_id).where(
            AirframeSpell.value == "49d283")).scalar_one()
        session.add(AirframeEvent(
            airframe_id=frame_id, kind="squawk", source="live",
            visibility="held", detail={"code": "7500"},
            at=datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)))
        session.commit()
    body = client.get("/v1/airframes/49d283").json()
    h = body["history"]
    assert h["first_observed"] == "2025-08-28"
    assert [o["icao"] for o in h["operators"]] == ["TVS", "KNE", "TVS"]
    assert h["operators"][1]["name"] == "flynas"
    assert h["registrations"][0]["reg"] == "OK-TVY"
    assert h["events"][0]["kind"] == "operator_change"      # newest first
    assert all(e["kind"] != "squawk" for e in h["events"])  # held stays out
    # a hex the record does not hold: history is null, not an error
    with sm() as session:
        session.add(RefAirframe(hex="abc123", registration="N1", source="tar1090"))
        session.commit()
    assert client.get("/v1/airframes/abc123").json()["history"] is None
