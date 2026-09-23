#!/usr/bin/env python3
"""Rebuild api/refdata/pac_airways.json: the fixed Pacific oceanic routes.

The FAA's 28-day NASR subscription (US government data, public domain)
lists the Pacific oceanic airways, AWY_BASE rows designated "PA": the
North Pacific routes between Alaska and Japan (R220, R580, A590, ...)
and the Central East Pacific routes between Hawaii and the mainland
(R576, R577, R578, ...). Each is a string of point names; FIX_BASE
places the named points and NAV_BASE the radio beacons.

    python3 tools/pac_airways.py 03_Sep_2026_CSV.zip

(the zip on disk or its URL). Writes {airway: [[name, lat, lon], ...]}.
"""
import csv
import io
import json
import math
import os
import sys
import urllib.request
import zipfile

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "api", "refdata", "pac_airways.json")


def _rows(z, name):
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8"))


def _km(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    c = (math.sin((p2 - p1) / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(c)))


def places(z):
    """{name: [(lat, lon), ...]}; a beacon ident can repeat worldwide."""
    out = {}
    for fname, key in (("FIX_BASE.csv", "FIX_ID"), ("NAV_BASE.csv", "NAV_ID")):
        for r in _rows(z, fname):
            if r["LAT_DECIMAL"] and r["LONG_DECIMAL"]:
                pos = (float(r["LAT_DECIMAL"]), float(r["LONG_DECIMAL"]))
                seen = out.setdefault(r[key], [])
                if pos not in seen:
                    seen.append(pos)
    return out


def resolve(names, where):
    """Place each point of one airway; a name with several places takes
    the one nearest its placed neighbours. None if any point is unknown."""
    pts = [where.get(n) or [] for n in names]
    if not all(pts):
        return None
    chosen = [c[0] if len(c) == 1 else None for c in pts]
    for _ in range(len(names)):
        for i, c in enumerate(pts):
            if chosen[i] is not None:
                continue
            near = [chosen[j] for j in (i - 1, i + 1)
                    if 0 <= j < len(chosen) and chosen[j] is not None]
            if near:
                chosen[i] = min(c, key=lambda p: min(_km(p, q) for q in near))
    if any(p is None for p in chosen):
        return None
    return [[n, round(p[0], 5), round(p[1], 5)] for n, p in zip(names, chosen)]


def build(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        where = places(z)
        airways, skipped = {}, []
        for r in _rows(z, "AWY_BASE.csv"):
            if r["AWY_DESIGNATION"] != "PA":
                continue
            names = r["AIRWAY_STRING"].split()
            placed = resolve(names, where) if len(names) >= 2 else None
            if placed:
                airways[r["AWY_ID"]] = placed
            else:
                skipped.append(r["AWY_ID"])
    return airways, skipped


def main(src):
    raw = (open(src, "rb").read() if os.path.exists(src) else
           urllib.request.urlopen(src, timeout=300).read())
    airways, skipped = build(raw)
    with open(OUT, "w") as fh:
        json.dump(dict(sorted(airways.items())), fh, separators=(",", ":"))
        fh.write("\n")
    print("pac_airways.json: %d airways, %d points%s" % (
        len(airways), sum(len(v) for v in airways.values()),
        "; skipped (unplaced point): " + " ".join(skipped) if skipped else ""))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
