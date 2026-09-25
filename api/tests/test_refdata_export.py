"""The read-only snapshot: every exported table holds exactly the
source's rows, the tables written through the day go in only as public
cuts, and a failed export never replaces a good file."""
import json
import sqlite3

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app import refdata_export
from test_refdata import _seed_all


def _source(tmp_path):
    url = "sqlite:///%s" % (tmp_path / "source.db")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    _seed_all(sessionmaker(bind=engine, expire_on_commit=False), tmp_path)
    return url, engine


def _rows(conn, table):
    return sorted(json.dumps(list(r), default=str)
                  for r in conn.execute(select(table)))


def test_export_copies_every_row(tmp_path):
    url, engine = _source(tmp_path)
    out = tmp_path / "refdata.sqlite"
    counts = refdata_export.export(url, str(out), log=lambda *a: None)
    public = {p[0].name for p in refdata_export.PUBLIC}
    assert set(counts) == set(refdata_export.TABLES) | public
    assert counts["ref_airframes"] == 3 and counts["ref_airports"] == 3
    dst = create_engine("sqlite:///%s" % out)
    with engine.connect() as a, dst.connect() as b:
        for name in refdata_export.TABLES:
            if name in refdata_export.ROW_FILTERS:
                continue
            t = Base.metadata.tables[name]
            assert _rows(a, t) == _rows(b, t), name
    con = sqlite3.connect(out)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "stations" not in names and "live_state" not in names
    ranks = [k for k, in con.execute(
        "SELECT key FROM rank_ref_airlines_icao ORDER BY rank")]
    assert ranks == sorted(ranks) and len(ranks) == counts["ref_airlines"]
    meta = dict(con.execute("SELECT key, value FROM snapshot_meta"))
    assert '"ref_airframes": 3' in meta["tables"] and meta["created_at"]


def test_a_failed_export_keeps_the_last_good_file(tmp_path, monkeypatch):
    url, _ = _source(tmp_path)
    out = tmp_path / "refdata.sqlite"
    out.write_bytes(b"last good")
    monkeypatch.setattr(refdata_export, "TABLES",
                        refdata_export.TABLES + ("no_such_table",))
    with pytest.raises(KeyError):
        refdata_export.export(url, str(out), log=lambda *a: None)
    assert out.read_bytes() == b"last good"


def test_only_public_cuts_of_the_day_tables(tmp_path):
    import datetime
    from app.refdata_models import (Airframe, AirframeEvent, Claim,
                                    Endorsement, RouteCatalog)
    url, engine = _source(tmp_path)
    now = datetime.datetime(2026, 9, 25, 3, 0, 5, 760215,
                            tzinfo=datetime.timezone.utc)
    with sessionmaker(bind=engine)() as s:
        s.add(Claim(id=1, callsign="SIA1", origin="SIN", dest="LHR",
                    status="approved", verdict="corroborated",
                    anonymous_count=0, first_at=now, last_at=now,
                    reviewed_at=now, reviewed_by="op",
                    review_note="private note", checks={"x": 1}))
        s.add(Endorsement(id=1, claim_id=1, handle="ann", note="private",
                          key_name="k", received_at=now))
        s.add(Endorsement(id=2, claim_id=1, handle=None, note="anon",
                          received_at=now))
        s.add(RouteCatalog(id=1, callsign="SIA1", origin="SIN", dest="LHR",
                           via=["DXB"], valid_from=now.date(),
                           source="community", claim_id=1, approved_at=now))
        frame = Airframe(first_observed=now.date(), last_observed=now.date(),
                         created_at=now, updated_at=now)
        s.add(frame)
        s.flush()
        for vis in ("private", "public"):
            s.add(AirframeEvent(airframe_id=frame.id, kind="squawk", at=now,
                                source=vis, visibility=vis,
                                evidence_ref="secret"))
        s.commit()
    out = tmp_path / "refdata.sqlite"
    refdata_export.export(url, str(out), log=lambda *a: None)
    con = sqlite3.connect(out)
    cols = lambda t: [r[1] for r in con.execute("PRAGMA table_info(%s)" % t)]
    assert "review_note" not in cols("claims") and "checks" not in cols("claims")
    assert cols("endorsements") == ["id", "claim_id", "handle"]
    assert [r for r in con.execute("SELECT handle FROM endorsements")] == [("ann",)]
    assert con.execute("SELECT callsign, via FROM route_catalog").fetchall() \
        == [("SIA1", '["DXB"]')]
    assert con.execute("SELECT visibility, evidence_ref FROM airframe_events"
                       " WHERE source IN ('private', 'public')").fetchall() \
        == [("public", None)]
