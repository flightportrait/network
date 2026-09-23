"""Score the position estimator against a day of real coverage gaps.

Every gap in a readsb trace is an aircraft that left the network's
coverage and came back: the point before the gap is what the estimator
had, the point after it is where the aircraft really was. For every gap
where the estimator would have drawn the aircraft, each method places
it at the moment it reappeared and the distance to the truth is the
error. Run nightly on yesterday's traces and recorded, so the accuracy
/v1/estimated claims is always the measured one.

    python -m app.estimate_score --day 2026-09-22 \\
        --base http://AGGREGATOR:8090/globe_history

The day's aircraft come from readsb's heatmaps (a whole-sky snapshot
every 15 s, fixed file names); their traces are then fetched one by one,
since the history is not listable. tools/estimate_backtest.py scores a
local directory of traces with the same code.
"""
import argparse
import concurrent.futures
import datetime
import gzip
import json
import statistics
import struct
import sys
import urllib.error
import urllib.request

from . import estimate as E

MIN_GAP_S = 300
METHODS = ("dr", "gc", "converge")
BUCKETS = ((5, 15), (15, 30), (30, 60), (60, 120), (120, 480))
HEATMAP_MARK = 0x0E7F7C9D


# ---- traces ------------------------------------------------------------

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


def score(traces, route_of, airports):
    """traces: iterable of readsb trace dicts. route_of(callsign) ->
    [IATA, ...] or None; airports: {IATA: (lat, lon)}. Returns
    (counts, {method: [(gap_minutes, error_km), ...]})."""
    results = {m: [] for m in METHODS}
    counts = {"traces": 0, "gaps": 0, "cruising": 0, "routed": 0,
              "estimated": 0, "landed_or_off": 0,
              "reacquired_en_route": 0}
    for trace in traces:
        counts["traces"] += 1
        pts = list(points(trace))
        for a, b in zip(pts, pts[1:]):
            if b[0] - a[0] < MIN_GAP_S:
                continue
            counts["gaps"] += 1
            obs = {"lat": a[1], "lon": a[2], "alt": a[3], "gs": a[4],
                   "track": a[5]}
            if not E.eligible(obs):
                continue
            counts["cruising"] += 1
            dest = E.pick_destination(a[1], a[2], a[5],
                                      route_of(a[6]) if a[6] else None,
                                      airports)
            if not dest:
                continue
            counts["routed"] += 1
            dt = b[0] - a[0]
            if dt > E.horizon_s(obs, dest[3]):
                continue                  # the estimate would have ended
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
                    continue              # it would have stopped drawing
                results[m].append((dt / 60.0,
                                   E.haversine_km(la, lo, b[1], b[2])))
    return counts, results


def summary(counts, results):
    """Numbers worth recording: per method n, median, p75, p90 and the
    median per gap bucket."""
    out = {"counts": counts, "methods": {}}
    for m, rows in results.items():
        errs = sorted(r[1] for r in rows)
        if not errs:
            out["methods"][m] = {"n": 0}
            continue
        buckets = {}
        for lo, hi in BUCKETS:
            sub = [r[1] for r in rows if lo <= r[0] < hi]
            if sub:
                buckets["%d-%d" % (lo, hi)] = {
                    "n": len(sub), "median_km": round(statistics.median(sub), 1)}
        out["methods"][m] = {
            "n": len(errs), "median_km": round(statistics.median(errs), 1),
            "p75_km": round(errs[int(len(errs) * .75)], 1),
            "p90_km": round(errs[int(len(errs) * .9)], 1),
            "by_gap_min": buckets}
    return out


# ---- fetching a day from the aggregator ---------------------------------

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": "fp-estimate-score", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return gzip.decompress(raw)
    except OSError:
        return raw


def day_hexes(base, day):
    """ICAO addresses in the day's heatmaps (non-ICAO addresses carry
    type bits above the 24-bit address and have no plain trace)."""
    hexes, rec = set(), struct.Struct("<Iiihh")
    for n in range(48):
        url = "%s/%s/heatmap/%02d.bin.ttf" % (base, day.strftime("%Y/%m/%d"), n)
        try:
            b = _get(url)
        except (urllib.error.URLError, OSError):
            continue
        for i in range(len(b) // 16):
            addr, lat = rec.unpack_from(b, i * 16)[:2]
            if addr == HEATMAP_MARK or addr >> 24:
                continue
            hexes.add("%06x" % addr)
    return hexes


def day_traces(base, day, workers=16):
    """Yield the day's trace dicts, fetched concurrently."""
    path = day.strftime("%Y/%m/%d")

    def one(hex_id):
        url = "%s/%s/traces/%s/trace_full_%s.json" % (base, path, hex_id[-2:], hex_id)
        try:
            return json.loads(_get(url))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        for trace in pool.map(one, sorted(day_hexes(base, day))):
            if trace:
                yield trace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--day", help="UTC day, default yesterday")
    parser.add_argument("--base", required=True,
                        help="the aggregator's globe_history URL")
    parser.add_argument("--no-record", action="store_true",
                        help="print only, write nothing")
    args = parser.parse_args(argv)
    day = (datetime.date.fromisoformat(args.day) if args.day else
           datetime.datetime.now(datetime.timezone.utc).date()
           - datetime.timedelta(days=1))

    from .main import create_network_api_app
    from .routes_live import airport_coords, route_of
    from .settings import Settings
    app = create_network_api_app(Settings(), start_pollers=False)
    counts, results = score(day_traces(args.base.rstrip("/"), day),
                            lambda cs: route_of(app, cs), airport_coords(app))
    doc = summary(counts, results)
    print(json.dumps({"day": day.isoformat(), **doc}))
    if counts["traces"] == 0:
        print("estimate_score: no traces for %s, nothing recorded" % day,
              file=sys.stderr)
        return 1
    if not args.no_record:
        from .refdata_models import EstimateScore
        with app.state.sessionmaker() as session:
            session.merge(EstimateScore(day=day, detail=doc))
            session.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
