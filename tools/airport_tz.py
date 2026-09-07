#!/usr/bin/env python3
"""Attach an Olson timezone to each web/assets/airports.json entry.

Adds a sixth element, the timezone name, so the pages can show times in
each airport's local time. Source: OpenFlights airports.dat (ODbL),
matched by IATA then ICAO. Airports OpenFlights lists without a zone
take the zone of the nearest airport within 300 km that has one.
Idempotent: re-run after refreshing the asset.

    python3 tools/airport_tz.py [path/to/airports.dat]
"""
import csv
import json
import math
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AIRPORTS = os.path.join(ROOT, "web", "assets", "airports.json")
SOURCE = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"


def load_openflights(path):
    if path:
        fh = open(path, encoding="utf-8")
    else:
        fh = urllib.request.urlopen(SOURCE, timeout=120)
        fh = (line.decode("utf-8") for line in fh)
    by_code, zoned = {}, []
    for row in csv.reader(fh):
        if len(row) < 12:
            continue
        iata, icao, tz = row[4].strip(), row[5].strip(), row[11].strip()
        if "/" not in tz:
            continue
        try:
            lat, lon = float(row[6]), float(row[7])
        except ValueError:
            continue
        if len(iata) == 3 and iata != "\\N":
            by_code[iata] = tz
        if len(icao) == 4 and icao != "\\N":
            by_code[icao] = tz
        zoned.append((lat, lon, tz))
    return by_code, zoned


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(a))


def nearest(zoned, lat, lon):
    best, best_d = None, 300.0
    for zlat, zlon, tz in zoned:
        if abs(zlat - lat) > 3 or abs(zlon - lon) > 3:
            continue
        d = km(lat, lon, zlat, zlon)
        if d < best_d:
            best, best_d = tz, d
    return best


def main():
    by_code, zoned = load_openflights(sys.argv[1] if len(sys.argv) > 1 else None)
    with open(AIRPORTS) as fh:
        airports = json.load(fh)
    direct = near = missing = 0
    for code, a in airports.items():
        tz = by_code.get(code)
        if tz:
            direct += 1
        else:
            tz = nearest(zoned, a[1], a[2])
            if tz:
                near += 1
            else:
                missing += 1
        while len(a) < 5:
            a.append(None)
        if len(a) == 5:
            a.append(tz)
        else:
            a[5] = tz
    with open(AIRPORTS, "w") as fh:
        json.dump(airports, fh, separators=(",", ":"), ensure_ascii=False)
    print(f"{direct} matched, {near} from a neighbour, {missing} without a zone")


if __name__ == "__main__":
    main()
