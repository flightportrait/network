#!/usr/bin/env python3
"""Score North Atlantic track estimates against crossings on disk.

The same scoring the nightly runs (api/app/nat_score.py), over crossings
kept by tools/nat_flights.py and a file of track messages valid that day
(app.nat.parse_parts output, the FAA JSON itself, or an export of
nat_messages):

    python3 tools/nat_backtest.py CROSSINGS_DIR messages.json \\
        routes.json.gz airports.json [--match-deg 0.5]
"""
import argparse
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "api"))

from app import estimate as E                      # noqa: E402
from app import nat                                # noqa: E402
from app import nat_score as N                     # noqa: E402


def load(path):
    raw = open(path, "rb").read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw)


def messages_from(doc):
    if doc and isinstance(doc, list) and "condition_message" in doc[0]:
        return nat.parse_parts(doc)
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("crossings")
    ap.add_argument("messages")
    ap.add_argument("routes")
    ap.add_argument("airports")
    ap.add_argument("--match-deg", type=float, default=E.NAT_MATCH_DEG)
    args = ap.parse_args(argv)
    E.NAT_MATCH_DEG = args.match_deg
    routes = load(args.routes)
    airports = {k: (v[1], v[2]) for k, v in load(args.airports).items()
                if isinstance(v, list) and len(v) > 2}
    traces = (load(os.path.join(args.crossings, f))
              for f in sorted(os.listdir(args.crossings)))
    counts, rows = N.score(traces, messages_from(load(args.messages)),
                           routes.get, airports)
    doc = N.summary(counts, rows)
    print(json.dumps(doc, indent=1))
    return doc


if __name__ == "__main__":
    main()
