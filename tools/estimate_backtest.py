#!/usr/bin/env python3
"""Score the position estimator against a local directory of traces.

The same scoring the nightly runs (api/app/estimate_score.py), over
files on disk instead of the aggregator:

    python3 tools/estimate_backtest.py TRACES_DIR routes.json.gz airports.json

TRACES_DIR: readsb globe_history traces (trace_full_*.json, gzipped or
not). routes.json.gz: the routes artifact ({callsign: [IATA, ...]}).
airports.json: web/assets/airports.json ({IATA: [city, lat, lon, ...]}).
"""
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "api"))

from app import estimate_score as S                # noqa: E402


def load(path):
    raw = open(path, "rb").read()
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass
    return json.loads(raw)


def traces(tdir):
    for root, _, files in os.walk(tdir):
        for f in files:
            if f.startswith("trace_full_"):
                yield load(os.path.join(root, f))


def main(tdir, routes_path, airports_path):
    routes = load(routes_path)
    airports = {k: (v[1], v[2]) for k, v in load(airports_path).items()
                if isinstance(v, list) and len(v) > 2}
    counts, results = S.score(traces(tdir), routes.get, airports)
    print(json.dumps(S.summary(counts, results), indent=1))
    return counts, results


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
