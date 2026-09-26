"""Schedule backtest: which way of reading the timetable predicts
tomorrow best.

For each of the newest --days days in the flight log, the schedule is
read from the --window days BEFORE it (never the day itself, unlike
schedule_quality, which scores the table the nightly just wrote), then
every leg flown that day is scored: did the predicted local departure
fall within 15, 30 and 45 minutes of the observed one. Every method in
schedule_pick runs on the same legs. Read-only; prints a table.

    python -m app.schedule_backtest [--days 7] [--window 60] [--legs data/legs.db]
"""
import argparse
import datetime
import os
import sqlite3
import statistics
from array import array

from sqlalchemy import select

from .refdata_ingest import _local_day_min
from .refdata_models import RefAirport
from .schedule_pick import METHODS, circ_diff, pick

TOLERANCES = (15, 30, 45)
SUPPORT = ((1, 1, "1 sighting"), (2, 4, "2-4"), (5, 10 ** 9, "5+"))


def load(conn, tz, since):
    """{(callsign, org, dst): array of day*1440+minute} from `since` on,
    day and minute local to the origin. Compact: millions of legs, flat ints."""
    out, zones = {}, {}
    for cs, org, dst, date, dep_ts in conn.execute(
            "SELECT callsign, org, dst, date, dep_ts FROM legs"
            " WHERE date >= ? AND callsign IS NOT NULL AND callsign <> ''"
            " AND org IS NOT NULL AND dst IS NOT NULL AND org <> dst"
            " AND dep_ts IS NOT NULL"
            " AND (arr_ts IS NULL OR arr_ts - dep_ts >= 600)", (since,)):
        local = _local_day_min(dep_ts, tz.get(org), zones)
        if local is None:
            continue
        day, minute = local                 # the origin's own calendar
        key = (cs.strip().upper(), org, dst)
        seen = out.get(key)
        if seen is None:
            seen = out[key] = array("i")
        seen.append(day * 1440 + minute)
    return out


def score(legs, days, window, methods=METHODS):
    """{method: {tolerance: hits, "n": scored, "errors": [...], support
    buckets}} plus the counts, over the last `days` days in `legs`."""
    last = max(v // 1440 for seen in legs.values() for v in seen)
    first = last - days + 1
    result = {m: {"n": 0, "hits": {t: 0 for t in TOLERANCES}, "errors": [],
                  "support": {label: [0, 0] for _, _, label in SUPPORT}}
              for m in methods}
    flown = unseen = 0
    for seen in legs.values():
        by_day = [(v // 1440, v % 1440) for v in seen]
        for day, minute in by_day:
            if day < first:
                continue
            flown += 1
            train = [s for s in by_day if day - window <= s[0] < day]
            if not train:
                unseen += 1
                continue
            weekday = (day - 1) % 7
            n = len(train)
            bucket = next(label for lo, hi, label in SUPPORT if lo <= n <= hi)
            for m in methods:
                predicted = pick(train, day - 1, m, weekday)
                err = circ_diff(minute, predicted)
                r = result[m]
                r["n"] += 1
                r["errors"].append(err)
                for t in TOLERANCES:
                    if err <= t:
                        r["hits"][t] += 1
                r["support"][bucket][0] += 1
                if err <= 45:
                    r["support"][bucket][1] += 1
    return {"first": datetime.date.fromordinal(first).isoformat(),
            "last": datetime.date.fromordinal(last).isoformat(),
            "flown": flown, "unseen": unseen, "methods": result}


def report(res):
    print("— schedule backtest: %s to %s, each day predicted from the days"
          " before it —" % (res["first"], res["last"]))
    print("  legs flown %d, no earlier sighting %d" % (res["flown"], res["unseen"]))
    print("  %-24s %8s %8s %8s %8s %8s" % ("method", "scored", "<=15",
                                           "<=30", "<=45", "median"))
    for m, r in res["methods"].items():
        n = r["n"] or 1
        print("  %-24s %8d %7.1f%% %7.1f%% %7.1f%% %6d m" % (
            m, r["n"], *(100.0 * r["hits"][t] / n for t in TOLERANCES),
            statistics.median(r["errors"]) if r["errors"] else 0))
    print("  within 45 min, by sightings in the window:")
    for m, r in res["methods"].items():
        print("  %-24s %s" % (m, "   ".join(
            "%s %.1f%% of %d" % (label, 100.0 * hit / n if n else 0, n)
            for label, (n, hit) in r["support"].items())))


def main():
    from .db import make_sessionmaker
    from .settings import Settings
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--window", type=int, default=60)
    ap.add_argument("--legs", default=None, help="legs artifact (default: settings)")
    args = ap.parse_args()
    settings = Settings()
    path = args.legs or settings.legs_path
    if not os.path.exists(path):
        raise SystemExit("no legs artifact at %s" % path)
    session = make_sessionmaker(settings.database_url)()
    tz = {}
    for ident, iata, z in session.execute(
            select(RefAirport.ident, RefAirport.iata, RefAirport.tz)
            .where(RefAirport.tz.is_not(None))):
        if ident:
            tz[ident] = z
        if iata:
            tz.setdefault(iata, z)
    session.close()
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        hi = conn.execute("SELECT MAX(date) FROM legs WHERE org IS NOT NULL"
                          " AND dst IS NOT NULL AND dep_ts IS NOT NULL").fetchone()[0]
        since = (datetime.date.fromisoformat(hi)
                 - datetime.timedelta(days=args.days + args.window)).isoformat()
        legs = load(conn, tz, since)
    finally:
        conn.close()
    report(score(legs, args.days, args.window))


if __name__ == "__main__":
    main()
