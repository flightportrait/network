#!/usr/bin/env python3
"""Write web/assets/operators.json: ICAO operator code to name, for
every callsign prefix the sky can carry.

Source: Virtual Radar Server's standing data (CC0), the maintained
list of airline designators. The API's /v1/airlines names only the
operators the archive has seen fly a schedule; the aircraft card and
the list need a name for any prefix, business jets and one-offs
included. This file is that, loaded once the page is idle.

    python3 tools/operators_asset.py
"""
import csv
import io
import json
import os
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DST = os.path.join(ROOT, "web", "assets", "operators.json")
SRC = ("https://raw.githubusercontent.com/vradarserver/standing-data/"
       "main/airlines/schema-01/airlines.csv")


def main():
    with urllib.request.urlopen(SRC, timeout=60) as resp:
        text = resp.read().decode("utf-8-sig")
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        icao = (row.get("ICAO") or "").strip().upper()
        name = (row.get("Name") or "").strip()
        if len(icao) == 3 and icao.isalpha() and name:
            out[icao] = name
    with open(DST, "w") as fh:
        json.dump(dict(sorted(out.items())), fh, separators=(",", ":"),
                  ensure_ascii=False)
    print("%s: %d operators, %d bytes" % (
        os.path.relpath(DST, ROOT), len(out), os.path.getsize(DST)))


if __name__ == "__main__":
    main()
