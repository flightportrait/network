#!/usr/bin/env python3
"""Name Italian fields by the city they serve, not the comune they sit in.

OurAirports' municipality for Italy is the comune with its province in
brackets: Verona's airport is "Caselle (VR)", Milan Linate "Segrate (MI)",
Bergamo "Orio al Serio (BG)". The map and the cards want the city.
Rewrites the name in web/assets/airports.json: a hand list for the
fields whose comune is not the city, the province suffix dropped for
the rest. Idempotent.

    python3 tools/airport_city.py
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AIRPORTS = os.path.join(ROOT, "web", "assets", "airports.json")

CITY = {
    "VRN": "Verona", "LIPX": "Verona",
    "TRN": "Torino", "LIMF": "Torino",
    "LIN": "Milano", "LIML": "Milano",
    "MXP": "Milano", "LIMC": "Milano",
    "BGY": "Bergamo", "LIME": "Bergamo",
    "AOI": "Ancona", "LIPY": "Ancona",
    "VBS": "Brescia", "LIPO": "Brescia",
    "CUF": "Cuneo", "LIMZ": "Cuneo",
    "CRV": "Crotone", "LIBC": "Crotone",
    "EBA": "Elba", "LIRJ": "Elba",
}


def main():
    with open(AIRPORTS) as fh:
        data = json.load(fh)
    changed = 0
    for code, entry in data.items():
        name = entry[0] or ""
        new = CITY.get(code) or re.sub(r"\s*\(\w\w\)$", "", name)
        if new != name:
            entry[0] = new
            changed += 1
    with open(AIRPORTS, "w") as fh:
        json.dump(data, fh, separators=(",", ":"), ensure_ascii=False)
    print("%d names changed" % changed)


if __name__ == "__main__":
    main()
