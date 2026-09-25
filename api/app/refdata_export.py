"""Export the reference and history tables to one read-only SQLite file.

The tables here change only when the nightly writes them (registry,
airlines, airports, types, routes, schedule, leg stats, the airframe
history, the estimate scores). A server that only reads them — networkd,
or a self-hosted instance with no Postgres — opens this file instead of
the database. Tables written through the day (stations, contributions,
live estimates, NAT messages) are not part of it.

The file is built beside the target and renamed over it only when every
table's row count matches the source, so a reader always sees a whole
snapshot, never a half-written one.

    python -m app.refdata_export data/refdata.sqlite
"""
import datetime
import json
import os
import sqlite3
import sys
import time

from sqlalchemy import create_engine, func, insert, select

from .db import Base
from . import models, refdata_models  # noqa: F401 — register the tables
from .settings import Settings

TABLES = (
    "ref_airframes", "ref_airlines", "ref_alliances",
    "ref_alliance_memberships", "ref_types", "ref_airports", "ref_routes",
    "ref_airline_countries", "ref_leg_stats", "ref_schedule", "ref_imports",
    "airframes", "airframe_spells", "airframe_claims", "airframe_events",
    "estimate_scores",
)
BATCH = 5000

# Text orders a reader must reproduce exactly. Postgres sorts text by the
# database's collation (en_US), which SQLite does not have; the export
# asks Postgres for each order and stores it as ranks, one table per
# (table, key column, sort column): rank_<table>_<column>(key, rank).
# Dense ranks give equal values equal ranks, so the reader can leave
# their order to the same sort Postgres uses (networkd's pgsort).
RANKS = (
    ("ref_airlines", "icao", "icao", False),
    ("ref_alliances", "slug", "name", False),
    ("ref_airports", "ident", "ident", False),
    ("ref_airframes", "hex", "registration", True),
    ("ref_airports", "ident", "name", False),
    ("ref_airlines", "icao", "name", False),
)

# Tables computed by Postgres itself, for lookups whose answer depends on
# its rules: upper() is the database's (en_US: 'ß' stays 'ß', where
# Python's str.upper() writes 'SS'), and an airport's traffic is the
# departures search ranks by. (name, SQLite definition, Postgres query.)
DERIVED = (
    ("search_airports",
     "CREATE TABLE search_airports (ident TEXT PRIMARY KEY, name_upper TEXT,"
     " municipality_upper TEXT, traffic INTEGER NOT NULL)",
     "SELECT a.ident, upper(a.name), upper(a.municipality),"
     " coalesce((SELECT sum(s.n_flights) FROM ref_schedule s"
     " WHERE s.org = coalesce(a.iata, a.ident)), 0) FROM ref_airports a"),
    ("search_airlines",
     "CREATE TABLE search_airlines (icao TEXT PRIMARY KEY, name_upper TEXT)",
     "SELECT icao, upper(name) FROM ref_airlines"),
    ("search_types",
     "CREATE TABLE search_types (designator TEXT PRIMARY KEY, name_upper TEXT)",
     "SELECT designator, upper(name) FROM ref_types"),
    ("rank_callsign",
     "CREATE TABLE rank_callsign (key TEXT PRIMARY KEY, rank INTEGER NOT NULL)",
     "SELECT callsign, row_number() OVER (ORDER BY callsign) - 1"
     " FROM (SELECT DISTINCT callsign FROM ref_schedule) c"),
)

# Indexes for the reader's lookups that the Postgres schema does not
# carry (the airport page's boards, the published board's flights).
INDEXES = (
    "CREATE INDEX ix_snap_schedule_org_n ON ref_schedule (org, n_flights)",
    "CREATE INDEX ix_snap_schedule_dst_n ON ref_schedule (dst, n_flights)",
    "CREATE INDEX ix_snap_schedule_flight ON ref_schedule (flight)",
    "CREATE INDEX ix_snap_airports_iata ON ref_airports (iata)",
    "CREATE INDEX ix_snap_airframes_reg ON ref_airframes (registration)",
    "CREATE INDEX ix_snap_airframes_bare ON ref_airframes"
    " (replace(registration, '-', ''))",
    "CREATE INDEX ix_snap_search_airports_name ON search_airports (name_upper)",
    "CREATE INDEX ix_snap_search_airports_city ON search_airports"
    " (municipality_upper)",
    "CREATE INDEX ix_snap_search_airlines_name ON search_airlines (name_upper)",
)


def export(source_url: str, out_path: str, log=print) -> dict:
    """Copy TABLES from `source_url` into a new SQLite file at `out_path`.
    Returns {table: rows}."""
    src = create_engine(source_url)
    tmp = out_path + ".tmp"
    for leftover in (tmp, tmp + "-journal"):
        if os.path.exists(leftover):
            os.remove(leftover)
    dst = create_engine("sqlite:///" + tmp)
    tables = [Base.metadata.tables[t] for t in TABLES]
    Base.metadata.create_all(dst, tables=tables)
    counts = {}
    started = time.time()
    with src.connect() as s, dst.connect() as d:
        d.exec_driver_sql("PRAGMA journal_mode=OFF")
        d.exec_driver_sql("PRAGMA synchronous=OFF")
        for table in tables:
            t0 = time.time()
            n = 0
            rows = s.execution_options(stream_results=True, yield_per=BATCH) \
                .execute(select(table))
            for chunk in rows.partitions(BATCH):
                d.execute(insert(table), [dict(r._mapping) for r in chunk])
                n += len(chunk)
            d.commit()
            want = s.execute(select(func.count()).select_from(table)).scalar()
            got = d.execute(select(func.count()).select_from(table)).scalar()
            if not (want == got == n):
                raise RuntimeError("%s: source %d rows, copied %d, file %d"
                                   % (table.name, want, n, got))
            counts[table.name] = n
            log("  %-26s %9d rows  %5.1f s" % (table.name, n, time.time() - t0))
        for tname, key, col, dense in RANKS:
            t = Base.metadata.tables[tname]
            rank_table = "rank_%s_%s" % (tname, col)
            d.exec_driver_sql("CREATE TABLE %s (key TEXT PRIMARY KEY, "
                              "rank INTEGER NOT NULL)" % rank_table)
            order = t.c[col] if dense else (t.c[col], t.c[key])
            window = (func.dense_rank() if dense else func.row_number()) \
                .over(order_by=order)
            ranks = [(k, r - 1) for k, r in s.execute(select(t.c[key], window))]
            if ranks:
                d.exec_driver_sql("INSERT INTO %s VALUES (?, ?)" % rank_table,
                                  ranks)
            d.commit()
        for name, create, query in DERIVED:
            d.exec_driver_sql(create)
            rows = s.exec_driver_sql(query).fetchall()
            if rows:
                marks = ",".join("?" * len(rows[0]))
                d.exec_driver_sql("INSERT INTO %s VALUES (%s)" % (name, marks),
                                  [tuple(r) for r in rows])
            d.commit()
            log("  %-26s %9d rows" % (name, len(rows)))
        for sql in INDEXES:
            d.exec_driver_sql(sql)
        d.exec_driver_sql(
            "CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT)")
        meta = {
            "created_at": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds"),
            "tables": json.dumps(counts, sort_keys=True),
        }
        d.exec_driver_sql("INSERT INTO snapshot_meta VALUES (?, ?), (?, ?)",
                          ("created_at", meta["created_at"],
                           "tables", meta["tables"]))
        d.commit()
        d.exec_driver_sql("ANALYZE")
    dst.dispose()
    src.dispose()
    # compact, then check the file opens and holds what we wrote
    con = sqlite3.connect(tmp)
    con.execute("VACUUM")
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if ok != "ok":
        raise RuntimeError("integrity check: %s" % ok)
    os.replace(tmp, out_path)
    log("wrote %s: %d tables, %.1f MB, %.0f s" % (
        out_path, len(counts), os.path.getsize(out_path) / 1e6,
        time.time() - started))
    return counts


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    export(Settings().database_url, argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
