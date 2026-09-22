#!/usr/bin/env python3
"""Write web/assets/airports-map.json, the airports the map draws first.

airports.json keys every field under both its codes and carries the
name, position, tier, rank, timezone and role: the search index and
the airline pages want all of that. The sky layer at world and regional
zooms wants far less: one entry per field under its shortest code, the
top of the rank order (tools/rank_airports.py), name, position, tier
and rank. This file is that, a fifth of the size, loaded first; the
full file follows once the page is idle and the layer repaints with
everything. The rank-ordered thinning places the same hubs in the same
spots on both passes.

    python3 tools/airports_map.py [--top N]      # default 1000
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "web", "assets", "airports.json")
DST = os.path.join(ROOT, "web", "assets", "airports-map.json")


def main(top):
    with open(SRC) as fh:
        raw = json.load(fh)
    by_field = {}
    for code, a in raw.items():
        key = (a[1], a[2])
        prev = by_field.get(key)
        if prev is None or len(code) < len(prev[0]):
            by_field[key] = (code, a)
    kept = sorted(by_field.values(), key=lambda t: t[1][4] or 10 ** 6)[:top]
    out = {code: [a[0], a[1], a[2], a[3], a[4]] for code, a in kept}
    with open(DST, "w") as fh:
        json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
    print("%s: %d fields of %d, %d bytes" % (
        os.path.relpath(DST, ROOT), len(out), len(by_field),
        os.path.getsize(DST)))


if __name__ == "__main__":
    n = 1000
    if len(sys.argv) > 2 and sys.argv[1] == "--top":
        n = int(sys.argv[2])
    main(n)
