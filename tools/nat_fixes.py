#!/usr/bin/env python3
"""Rebuild api/refdata/nat_fixes.csv: where the named ends of the North
Atlantic tracks are.

A track message gives its oceanic points as coordinates but its entry
and exit only by name (MALOT, LOMSI). The names are published, with
their coordinates, by the states around the ocean:

- west (Gander's side): the FAA's 28-day NASR subscription, FIX_BASE,
  which carries the Canadian coastal fixes; US government data, public
  domain;
- east: the UK AIP (ENR 4.4, Shanwick and the Reykjavik boundary) and
  the Irish AIP (ENR 4.4, Shannon's oceanic entry and exit points).

Only points inside the North Atlantic box are kept, which also drops
the same five letters reused elsewhere in the world. The eAIP URLs
change with each cycle; pass the current ones:

    python3 tools/nat_fixes.py --nasr 03_Sep_2026_CSV.zip \\
        --uk https://www.aurora.nats.co.uk/htmlAIP/Publications/2026-09-03-AIRAC/html/eAIP/EG-ENR-4.4-en-GB.html \\
        --ie https://www.airnav.ie/AIRAC_MAY_2026/2026-05-14-AIRAC/html/eAIP/EI-ENR-4.4-en-IE.html

--nasr takes the zip on disk or its URL.
"""
import argparse
import csv
import html
import io
import os
import re
import urllib.request
import zipfile

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "api", "refdata", "nat_fixes.csv")
BOX = (40.0, 72.0, -72.0, -5.0)        # lat min/max, lon min/max

_UK = re.compile(r"\b([A-Z]{5}) TDESIGNATED_POINT;CODE_ID;\d+ "
                 r"(\d{6}(?:\.\d+)?[NS]) TDESIGNATED_POINT;GEO_LAT;\d+ "
                 r"(\d{7}(?:\.\d+)?[EW])")
_IE = re.compile(r"\b([A-Z]{5}) (\d{6}(?:\.\d+)?[NS]) (\d{7}(?:\.\d+)?[EW])\b")


def _get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "flightportrait-network (+https://flightportrait.com/network/)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def dms(text):
    """'513000N' -> 51.5; '0150000W' -> -15.0 (seconds may carry decimals)."""
    hemi, body = text[-1], text[:-1]
    deg_len = 2 if hemi in "NS" else 3
    v = (int(body[:deg_len]) + int(body[deg_len:deg_len + 2]) / 60.0
         + float(body[deg_len + 2:]) / 3600.0)
    return -v if hemi in "SW" else v


def _text(raw):
    t = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace"))
    return re.sub(r"\s+", " ", html.unescape(t))


def aip(raw, pattern):
    return [(n, dms(la), dms(lo)) for n, la, lo in pattern.findall(_text(raw))]


def nasr(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        rows = csv.DictReader(io.TextIOWrapper(z.open("FIX_BASE.csv"),
                                               encoding="utf-8"))
        return [(r["FIX_ID"], float(r["LAT_DECIMAL"]), float(r["LONG_DECIMAL"]))
                for r in rows if r["LAT_DECIMAL"] and r["LONG_DECIMAL"]]


def inside(lat, lon):
    return BOX[0] <= lat <= BOX[1] and BOX[2] <= lon <= BOX[3]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--nasr", required=True)
    ap.add_argument("--uk", required=True)
    ap.add_argument("--ie", required=True)
    args = ap.parse_args(argv)
    raw = (open(args.nasr, "rb").read() if os.path.exists(args.nasr)
           else _get(args.nasr))
    sources = [("uk-aip", aip(_get(args.uk), _UK)),
               ("ie-aip", aip(_get(args.ie), _IE)),
               ("faa-nasr", nasr(raw))]
    fixes = {}
    for source, points in sources:     # first source wins a shared name
        for name, lat, lon in points:
            if inside(lat, lon) and name not in fixes:
                fixes[name] = (round(lat, 5), round(lon, 5), source)
    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "lat", "lon", "source"])
        for name in sorted(fixes):
            w.writerow([name, *fixes[name]])
    counts = {}
    for _, _, s in fixes.values():
        counts[s] = counts.get(s, 0) + 1
    print("nat_fixes.csv: %d fixes %s" % (len(fixes), counts))


if __name__ == "__main__":
    main()
