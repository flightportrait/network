#!/usr/bin/env python3
"""Attach a role to each web/assets/airports.json entry.

Adds a seventh element: commercial (scheduled service), general,
military, or closed, from OurAirports (public domain) by IATA then
ICAO, the same reading the API's reference table uses. Idempotent:
re-run after refreshing either the asset or the registry.

    python3 tools/airport_role.py [path/to/airports.csv]
"""
import csv
import io
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AIRPORTS = os.path.join(ROOT, "web", "assets", "airports.json")
SOURCE = "https://davidmegginson.github.io/ourairports-data/airports.csv"
MILITARY = (" air base", " airbase", " air force", " afb", " raf ",
            " naval air", " nas ", " army air", " military",
            " marine corps", " air station", " air national guard")


def role(kind, scheduled, name):
    kind = (kind or "").strip().lower()
    if kind == "closed":
        return "closed"
    if (scheduled or "").strip().lower() == "yes":
        return "commercial"
    padded = " %s " % re.sub(r"[^a-z ]", " ", (name or "").lower())
    if any(w in padded for w in MILITARY):
        return "military"
    return "general"


def main():
    if len(sys.argv) > 1:
        text = open(sys.argv[1], encoding="utf-8").read()
    else:
        with urllib.request.urlopen(SOURCE, timeout=120) as fh:
            text = fh.read().decode("utf-8")
    by_code = {}
    for row in csv.DictReader(io.StringIO(text)):
        r = role(row.get("type"), row.get("scheduled_service"), row.get("name"))
        for code in (row.get("iata_code"), row.get("ident")):
            code = (code or "").strip().upper()
            if code and code not in by_code:
                by_code[code] = r
    with open(AIRPORTS, encoding="utf-8") as fh:
        airports = json.load(fh)
    counts = {}
    for code, entry in airports.items():
        r = by_code.get(code.upper(), "general")
        entry[6:] = [r]
        counts[r] = counts.get(r, 0) + 1
    with open(AIRPORTS, "w", encoding="utf-8") as fh:
        json.dump(airports, fh, separators=(",", ":"), sort_keys=True)
    print(", ".join("%s %d" % kv for kv in sorted(counts.items())))


if __name__ == "__main__":
    main()
