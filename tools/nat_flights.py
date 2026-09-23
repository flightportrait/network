#!/usr/bin/env python3
"""Keep a day's North Atlantic crossings from the adsb.lol archive.

The same streaming the nightly score runs (api/app/nat_score.py), with
the crossings written to OUT_DIR for tools/nat_backtest.py:

    python3 tools/nat_flights.py 2026-09-23 OUT_DIR
"""
import datetime
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "api"))

from app import nat_score as N                     # noqa: E402


def main(day, out):
    os.makedirs(out, exist_ok=True)
    kept = 0
    for name, trace in N.crossings(datetime.date.fromisoformat(day)):
        kept += 1
        with gzip.open(os.path.join(out, os.path.basename(name)
                                    .replace(".json", ".json.gz")), "wt") as fh:
            json.dump(trace, fh, separators=(",", ":"))
    print("done: %d North Atlantic crossings kept" % kept)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
