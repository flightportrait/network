"""Schedule quality: how well the schedule table's times agree with
what flew, on the newest day of the flight log — published times
(from airports' boards) and inferred times (from observation) scored
side by side, same legs, same tolerance.

    python -m app.schedule_quality [--tolerance 30] [--legs data/legs.db] [--day YYYY-MM-DD]
"""
import argparse
import datetime
import os
import sqlite3
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .refdata_models import RefAirport, RefSchedule


def _circ_diff(a, b):
    return abs(((a - b + 720) % 1440) - 720)


def _pct(n, d):
    return "%.1f%%" % (100.0 * n / d) if d else "n/a"


def score(session, legs_path, tolerance_min=45, day=None):
    """For each observed leg of the day with a scheduled row for its
    callsign and leg: was the row's departure time within tolerance of
    the observed departure? Grouped by where the time came from."""
    if not os.path.exists(legs_path):
        return None
    conn = sqlite3.connect("file:%s?mode=ro" % legs_path, uri=True)
    try:
        if day is None:
            day = conn.execute("SELECT MAX(date) FROM legs WHERE org IS NOT NULL"
                               " AND dst IS NOT NULL AND dep_ts IS NOT NULL").fetchone()[0]
        if not day:
            return None
        legs = conn.execute(
            "SELECT callsign, org, dst, dep_ts FROM legs WHERE date = ? AND org IS NOT NULL"
            " AND dst IS NOT NULL AND dep_ts IS NOT NULL AND callsign IS NOT NULL",
            (day,)).fetchall()
    finally:
        conn.close()
    tz = {}
    for iata, z in session.execute(
            select(RefAirport.iata, RefAirport.tz)
            .where(RefAirport.iata.is_not(None), RefAirport.tz.is_not(None))):
        tz.setdefault(iata, z)
    names = {leg[0] for leg in legs}
    sched = {}
    for r in session.execute(select(RefSchedule).where(RefSchedule.callsign.in_(names),
                                                       RefSchedule.dep_min.is_not(None))).scalars():
        sched[(r.callsign, r.org, r.dst)] = r
    m = {"day": str(day), "legs": len(legs), "tolerance_min": tolerance_min,
         "published": {"legs": 0, "hits": 0}, "inferred": {"legs": 0, "hits": 0}}
    for callsign, org, dst, dep_ts in legs:
        r = sched.get((callsign, org, dst))
        z = tz.get(org)
        if r is None or not z:
            continue
        try:
            local = datetime.datetime.fromtimestamp(int(dep_ts), ZoneInfo(z))
        except (ValueError, KeyError, OverflowError):
            continue
        observed = local.hour * 60 + local.minute
        bucket = m["published"] if r.source in ("both", "published") else m["inferred"]
        bucket["legs"] += 1
        if _circ_diff(observed, r.dep_min) <= tolerance_min:
            bucket["hits"] += 1
    for k in ("published", "inferred"):
        m[k]["accuracy"] = _pct(m[k]["hits"], m[k]["legs"])
    scored = m["published"]["legs"] + m["inferred"]["legs"]
    m["published_share"] = _pct(m["published"]["legs"], scored)
    m["scheduled_share"] = _pct(scored, m["legs"])
    return m


def main():
    from .db import make_sessionmaker
    from .settings import Settings
    ap = argparse.ArgumentParser()
    ap.add_argument("--tolerance", type=int, default=45)
    ap.add_argument("--legs", default=None, help="legs artifact (default: settings)")
    ap.add_argument("--day", default=None, help="YYYY-MM-DD (default: newest in the log)")
    args = ap.parse_args()
    settings = Settings()
    session = make_sessionmaker(settings.database_url)()
    m = score(session, args.legs or settings.legs_path, args.tolerance, args.day)
    print("— schedule times against what flew (%d min tolerance) —" % args.tolerance)
    if m is None:
        print("  no legs to score")
    else:
        for k, v in m.items():
            print("  %-22s %s" % (k, v))
    session.close()


if __name__ == "__main__":
    main()
