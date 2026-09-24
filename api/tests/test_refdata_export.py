"""The read-only snapshot: every exported table holds exactly the
source's rows, the tables written through the day stay out, and a failed
export never replaces a good file."""
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
    assert set(counts) == set(refdata_export.TABLES)
    assert counts["ref_airframes"] == 3 and counts["ref_airports"] == 3
    dst = create_engine("sqlite:///%s" % out)
    with engine.connect() as a, dst.connect() as b:
        for name in refdata_export.TABLES:
            t = Base.metadata.tables[name]
            assert _rows(a, t) == _rows(b, t), name
    con = sqlite3.connect(out)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "stations" not in names and "route_catalog" not in names
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
