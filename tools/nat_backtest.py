#!/usr/bin/env python3
"""Score North Atlantic track estimates against real ocean crossings.

For each crossing kept by tools/nat_flights.py, the ocean gap (the
aircraft lost on one side, heard again on the other) is scored twice at
the moment it reappeared: the current estimator (hold the track, then
turn toward the destination) and the track method (fly the day's
published North Atlantic track it matches, then on to the destination;
the current estimator where no track matches). The messages must be the
ones valid on the day of the flights.

    python3 tools/nat_backtest.py CROSSINGS_DIR messages.json \\
        routes.json.gz airports.json [--match-deg 0.5]

messages.json: parsed track messages (app.nat.parse_parts output, or
the FAA JSON itself, or an export of nat_messages).
"""
import argparse
import datetime
import gzip
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "api"))

from app import estimate as E                      # noqa: E402
from app import estimate_score as S                # noqa: E402
from app import nat                                # noqa: E402

MIN_GAP_S = 1200


def load(path):
    raw = open(path, "rb").read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw)


def messages_from(doc):
    """Parsed messages from any of: parsed list, FAA JSON, DB export."""
    if doc and isinstance(doc, list) and "condition_message" in doc[0]:
        return nat.parse_parts(doc)
    out = []
    for m in doc:
        vf, vt = m["valid_from"], m["valid_to"]
        out.append(dict(m, valid_from=str(vf).replace(" ", "T").replace("+00:00", "Z"),
                        valid_to=str(vt).replace(" ", "T").replace("+00:00", "Z")))
    return out


def ocean_gaps(pts):
    for a, b in zip(pts, pts[1:]):
        if b[0] - a[0] < MIN_GAP_S:
            continue
        if not all(isinstance(p[3], (int, float)) and p[3] >= 25000 and
                   40 <= p[1] <= 70 for p in (a, b)):
            continue
        if abs(a[2] - b[2]) < 5 or max(a[2], b[2]) > 5 or min(a[2], b[2]) < -75:
            continue
        yield a, b


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("crossings")
    ap.add_argument("messages")
    ap.add_argument("routes")
    ap.add_argument("airports")
    ap.add_argument("--match-deg", type=float, default=E.NAT_MATCH_DEG)
    args = ap.parse_args(argv)
    E.NAT_MATCH_DEG = args.match_deg
    msgs = messages_from(load(args.messages))
    routes = load(args.routes)
    airports = {k: (v[1], v[2]) for k, v in load(args.airports).items()
                if isinstance(v, list) and len(v) > 2}
    rows = []
    counts = {"crossings": 0, "gaps": 0, "routed": 0, "in_track_hours": 0,
              "matched": 0}
    for f in sorted(os.listdir(args.crossings)):
        trace = load(os.path.join(args.crossings, f))
        counts["crossings"] += 1
        pts = list(S.points(trace))
        for a, b in ocean_gaps(pts):
            counts["gaps"] += 1
            obs = {"lat": a[1], "lon": a[2], "alt": a[3], "gs": a[4] or 0,
                   "track": a[5]}
            if obs["track"] is None or not obs["gs"]:
                continue
            dest = E.pick_destination(a[1], a[2], a[5],
                                      routes.get(a[6] or ""), airports)
            if not dest:
                continue
            counts["routed"] += 1
            dt = b[0] - a[0]
            when = datetime.datetime.fromtimestamp(a[0], datetime.timezone.utc)
            tracks = nat.active(msgs, when)
            if tracks:
                counts["in_track_hours"] += 1
            cla, clo, _, _ = E.project(obs, (dest[1], dest[2]), dt)
            cur = E.haversine_km(cla, clo, b[1], b[2])
            m = E.match_nat(obs, tracks) if tracks else None
            if m:
                counts["matched"] += 1
                nla, nlo, _, _ = E.project_path(obs, [tuple(p) for p in m] +
                                                [(dest[1], dest[2])], dt)
                trk = E.haversine_km(nla, nlo, b[1], b[2])
            else:
                trk = cur
            rows.append({"cs": a[6], "gap_min": round(dt / 60), "matched": bool(m),
                         "current_km": round(cur, 1), "track_km": round(trk, 1),
                         "dir": "W" if b[2] < a[2] else "E"})

    def med(xs):
        return round(statistics.median(xs), 1) if xs else None
    matched = [r for r in rows if r["matched"]]
    doc = {"counts": counts,
           "all": {"n": len(rows), "current_median_km": med([r["current_km"] for r in rows]),
                   "track_median_km": med([r["track_km"] for r in rows])},
           "matched": {"n": len(matched),
                       "current_median_km": med([r["current_km"] for r in matched]),
                       "track_median_km": med([r["track_km"] for r in matched]),
                       "track_better": sum(1 for r in matched if r["track_km"] < r["current_km"])},
           "worst_matched": sorted(matched, key=lambda r: r["track_km"] - r["current_km"])[-5:]}
    print(json.dumps(doc, indent=1))
    return doc


if __name__ == "__main__":
    main()
