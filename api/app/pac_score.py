"""Score the estimator over the Pacific against a day's real crossings.

For each flight that crossed the Pacific (from the adsb.lol archive,
ODbL): Asia to North America, Hawaii to the mainland, the South Pacific
to the Americas. Each ocean gap (lost on one side, heard again hours
later) is scored at the moment the aircraft reappeared, twice: the
current estimator, and the fixed-route method (fly the matched NOPAC or
CEPAC airway, app.pacific, then on to the destination; the current
estimator where no airway matches). Run in the nightly on yesterday;
recorded beside the day's estimate score under "pacific".

    python -m app.pac_score [--day 2026-09-23] [--keep DIR]
"""
import argparse
import datetime
import gzip
import io
import json
import os
import statistics
import sys
import tarfile

from . import estimate as E
from . import estimate_score as S
from . import pacific
from .nat_score import Parts

MIN_ALT = 25000
MIN_GAP_S = 1200
GAP_KT = (300, 650)            # the implied speed of one flight across a gap
BINS = (("0.3-1h", 0, 60), ("1-3h", 60, 180), ("3-6h", 180, 360),
        ("6-8h", 360, 480), ("8h+", 480, 10 ** 9))


def pacific_box(lat, lon):
    return -60 <= lat <= 66 and (lon >= 125 or lon <= -118)


def kind(trace):
    """'transpacific', 'hawaii', 'south' or None, from the cruise points."""
    pts = [p for p in trace.get("trace", [])
           if p[1] is not None and p[2] is not None
           and isinstance(p[3], (int, float)) and p[3] >= MIN_ALT]
    if len(pts) < 2:
        return None
    coast = any(-135 <= p[2] <= -115 for p in pts)
    if coast and any(p[1] < -10 for p in pts):
        return "south"                    # Australia, New Zealand, the islands
    asia = any(p[2] >= 135 and p[1] > 0 for p in pts)
    alaska = any(p[1] > 50 and p[2] < -140 for p in pts)
    if asia and (coast or alaska):
        return "transpacific"
    if coast and any(18 <= p[1] <= 23 and -161 <= p[2] <= -154 for p in pts):
        return "hawaii"
    return None


def crossings(day):
    """Yield (name, kind, trace) for the day's Pacific crossings, streamed."""
    stream = io.BufferedReader(Parts(day), buffer_size=1 << 20)
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for m in tar:
            if not m.isfile() or "trace_full_" not in m.name:
                continue
            raw = tar.extractfile(m).read()
            try:
                trace = json.loads(gzip.decompress(raw))
            except OSError:
                try:
                    trace = json.loads(raw)
                except ValueError:
                    continue
            except ValueError:
                continue
            k = kind(trace)
            if k:
                yield m.name, k, trace


def ocean_gaps(pts):
    """Gaps of one flight over the Pacific: both ends cruising, the
    implied speed a jet's, the midpoint over the ocean."""
    for a, b in zip(pts, pts[1:]):
        dt = b[0] - a[0]
        if dt < MIN_GAP_S:
            continue
        if not all(isinstance(p[3], (int, float)) and p[3] >= MIN_ALT for p in (a, b)):
            continue
        km = E.haversine_km(a[1], a[2], b[1], b[2])
        kt = km / (dt / 3600.0) / 1.852
        if not GAP_KT[0] <= kt <= GAP_KT[1]:
            continue
        mla, mlo = E.forward(a[1], a[2], E.bearing_deg(a[1], a[2], b[1], b[2]), km / 2)
        if pacific_box(mla, mlo):
            yield a, b


def score(traces, route_of, airports, airways):
    """traces: (kind, trace) pairs."""
    rows = []
    counts = {"crossings": 0, "gaps": 0, "routed": 0, "matched": 0}
    for k, trace in traces:
        counts["crossings"] += 1
        for a, b in ocean_gaps(list(S.points(trace))):
            counts["gaps"] += 1
            obs = {"lat": a[1], "lon": a[2], "alt": a[3], "gs": a[4] or 0,
                   "track": a[5]}
            if obs["track"] is None or not obs["gs"]:
                continue
            dest = E.pick_destination(a[1], a[2], a[5],
                                      route_of(a[6]) if a[6] else None, airports)
            if not dest:
                continue
            counts["routed"] += 1
            dt = b[0] - a[0]
            cla, clo, _, _ = E.project(obs, (dest[1], dest[2]), dt)
            cur = E.haversine_km(cla, clo, b[1], b[2])
            m = E.match_airway(obs, airways, (dest[1], dest[2]))
            if m:
                counts["matched"] += 1
                ala, alo, _, _ = E.project_path(obs, m + [(dest[1], dest[2])], dt)
                awy = E.haversine_km(ala, alo, b[1], b[2])
            else:
                awy = cur
            rows.append({"cs": a[6], "kind": k, "gap_min": round(dt / 60),
                         "dest": dest[0], "matched": bool(m),
                         "current_km": round(cur, 1), "airway_km": round(awy, 1)})
    return counts, rows


def summary(counts, rows):
    def med(xs):
        return round(statistics.median(xs), 1) if xs else None

    def block(rs):
        return {"n": len(rs),
                "current_median_km": med([r["current_km"] for r in rs]),
                "airway_median_km": med([r["airway_km"] for r in rs])}
    matched = [r for r in rows if r["matched"]]
    doc = {"counts": counts, "all": block(rows),
           "matched": dict(block(matched), airway_better=sum(
               1 for r in matched if r["airway_km"] < r["current_km"])),
           "by_kind": {k: block([r for r in rows if r["kind"] == k])
                       for k in ("transpacific", "hawaii", "south")},
           "by_gap": {name: block([r for r in rows if lo <= r["gap_min"] < hi])
                      for name, lo, hi in BINS},
           "worst_matched": sorted(matched, key=lambda r: r["airway_km"] -
                                   r["current_km"])[-5:]}
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", help="UTC day, default yesterday")
    ap.add_argument("--keep", help="also write the crossings here (gzip JSON)")
    ap.add_argument("--no-record", action="store_true")
    args = ap.parse_args(argv)
    day = (datetime.date.fromisoformat(args.day) if args.day else
           datetime.datetime.now(datetime.timezone.utc).date()
           - datetime.timedelta(days=1))
    from .main import create_network_api_app
    from .routes_live import airport_coords, route_of
    from .settings import Settings
    app = create_network_api_app(Settings(), start_pollers=False)

    def traces():
        for name, k, trace in crossings(day):
            if args.keep:
                os.makedirs(args.keep, exist_ok=True)
                with gzip.open(os.path.join(args.keep, k + "_" + os.path.basename(name)
                                            .replace(".json", ".json.gz")), "wt") as fh:
                    json.dump(trace, fh, separators=(",", ":"))
            yield k, trace

    counts, rows = score(traces(), lambda cs: route_of(app, cs),
                         airport_coords(app), pacific.airways())
    doc = summary(counts, rows)
    print(json.dumps({"day": day.isoformat(), **doc}))
    if counts["crossings"] == 0:
        print("pac_score: no crossings for %s, nothing recorded" % day,
              file=sys.stderr)
        return 1
    if not args.no_record:
        from .refdata_models import EstimateScore
        with app.state.sessionmaker() as session:
            row = session.get(EstimateScore, day)
            if row is None:
                session.add(EstimateScore(day=day, detail={"pacific": doc}))
            else:
                row.detail = dict(row.detail or {}, pacific=doc)
            session.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
