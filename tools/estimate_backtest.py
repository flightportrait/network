#!/usr/bin/env python3
"""Score the position estimator against real coverage gaps.

Every gap in a recorded readsb trace is an aircraft leaving the
network's coverage and coming back: the point before the gap is what the
estimator would have had, the point after it is where the aircraft
really was. For each gap where the estimator would have drawn the
aircraft, this places it with each method at the moment it reappeared
and measures the distance to the truth.

    python3 tools/estimate_backtest.py TRACES_DIR routes.json.gz airports.json

TRACES_DIR: readsb globe_history traces (trace_full_*.json, gzipped or
not). routes.json.gz: the routes artifact ({callsign: [IATA, ...]}).
airports.json: web/assets/airports.json ({IATA: [city, lat, lon, ...]}).
"""
import gzip
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "api"))

from app import estimate as E                      # noqa: E402

MIN_GAP_S = 300
METHODS = ("dr", "gc", "converge")
BUCKETS = ((5, 15), (15, 30), (30, 60), (60, 120), (120, 480))


def load(path):
    raw = open(path, "rb").read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw)


def points(trace):
    """(t, lat, lon, alt, gs, track, callsign) per trace point, the
    callsign carried forward from the last point that named it."""
    base, cs = trace.get("timestamp", 0), None
    for p in trace.get("trace", []):
        if len(p) > 8 and isinstance(p[8], dict) and p[8].get("flight"):
            cs = p[8]["flight"].strip().upper()
        if p[1] is None or p[2] is None:
            continue
        yield (base + p[0], p[1], p[2], p[3], p[4], p[5], cs)


def gaps(pts):
    for a, b in zip(pts, pts[1:]):
        if b[0] - a[0] >= MIN_GAP_S:
            yield a, b


def main(tdir, routes_path, airports_path):
    routes = load(routes_path)
    airports = {k: (v[1], v[2]) for k, v in load(airports_path).items()
                if isinstance(v, list) and len(v) > 2}
    results = {m: [] for m in METHODS}
    counts = {"gaps": 0, "cruising": 0, "routed": 0, "estimated": 0,
              "landed_or_off": 0, "reacquired_en_route": 0}
    for root, _, files in os.walk(tdir):
        for f in files:
            if not f.startswith("trace_full_"):
                continue
            pts = list(points(load(os.path.join(root, f))))
            for a, b in gaps(pts):
                counts["gaps"] += 1
                obs = {"lat": a[1], "lon": a[2], "alt": a[3], "gs": a[4],
                       "track": a[5]}
                if not E.eligible(obs):
                    continue
                counts["cruising"] += 1
                dest = E.pick_destination(a[1], a[2], a[5],
                                          routes.get(a[6] or ""), airports)
                if not dest:
                    continue
                counts["routed"] += 1
                dt = b[0] - a[0]
                if dt > E.horizon_s(obs, dest[3]):
                    continue          # the estimate would have ended
                counts["estimated"] += 1
                # the truth must be the same flight, still en route
                if not (isinstance(b[3], (int, float)) and b[3] >= 10000) or \
                        (b[6] and a[6] and b[6] != a[6]):
                    counts["landed_or_off"] += 1
                    continue
                counts["reacquired_en_route"] += 1
                for m in METHODS:
                    la, lo, _, rem = E.project(obs, (dest[1], dest[2]), dt, m)
                    if rem < E.STOP_BEFORE_KM:
                        continue      # it would have stopped drawing
                    err = E.haversine_km(la, lo, b[1], b[2])
                    results[m].append((dt / 60.0, err, a[6], f))
    print(json.dumps(counts))
    for m in METHODS:
        rows = results[m]
        if not rows:
            print(m, "no cases")
            continue
        errs = sorted(r[1] for r in rows)
        line = "%-9s n=%4d  median %6.1f km  p75 %6.1f  p90 %6.1f" % (
            m, len(errs), statistics.median(errs),
            errs[int(len(errs) * .75)], errs[int(len(errs) * .9)])
        for lo, hi in BUCKETS:
            sub = sorted(r[1] for r in rows if lo <= r[0] < hi)
            if sub:
                line += "  | %d-%dm n=%d med %.0f" % (
                    lo, hi, len(sub), statistics.median(sub))
        print(line)
    return results


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
