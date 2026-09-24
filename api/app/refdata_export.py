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
RANKS = (
    ("ref_airlines", "icao", "icao"),
    ("ref_alliances", "slug", "name"),
    ("ref_airports", "ident", "ident"),
)

# Indexes for the reader's lookups that the Postgres schema does not
# carry (the airport page's boards, the published board's flights).
INDEXES = (
    "CREATE INDEX ix_snap_schedule_org_n ON ref_schedule (org, n_flights)",
    "CREATE INDEX ix_snap_schedule_dst_n ON ref_schedule (dst, n_flights)",
    "CREATE INDEX ix_snap_schedule_flight ON ref_schedule (flight)",
    "CREATE INDEX ix_snap_airports_iata ON ref_airports (iata)",
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
        for tname, key, col in RANKS:
            t = Base.metadata.tables[tname]
            rank_table = "rank_%s_%s" % (tname, col)
            d.exec_driver_sql("CREATE TABLE %s (key TEXT PRIMARY KEY, "
                              "rank INTEGER NOT NULL)" % rank_table)
            ordered = s.execute(select(t.c[key]).order_by(t.c[col], t.c[key]))
            ranks = [(k, i) for i, (k,) in enumerate(ordered)]
            if ranks:
                d.exec_driver_sql("INSERT INTO %s VALUES (?, ?)" % rank_table,
                                  ranks)
            d.commit()
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
