#!/usr/bin/env python3
"""Rank the map's airports so the sky can thin them by zoom.

Adds a fifth element to each web/assets/airports.json entry — the
airport's rank, 1 = most prominent. Passengers first: the best annual
figure Wikidata holds for the IATA code (patronage, P3872; CC0). Fields
without one are placed from what the other assets already know — size
class and the runways in web/assets/runways.json, scaled to a passenger
guess that lands them below their surveyed peers. Idempotent: re-run
after refreshing either asset.

    python3 tools/rank_airports.py [--offline]

--offline skips Wikidata and ranks from size class and runways alone.
"""
import csv
import io
import json
import math
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AIRPORTS = os.path.join(ROOT, "web", "assets", "airports.json")
RUNWAYS = os.path.join(ROOT, "web", "assets", "runways.json")
SPARQL = "https://query.wikidata.org/sparql"
QUERY = ("SELECT ?iata (MAX(?n) AS ?pax) WHERE { ?a wdt:P238 ?iata . "
         "?a p:P3872 ?st . ?st ps:P3872 ?n . } GROUP BY ?iata")


def fetch_pax():
    url = SPARQL + "?" + urllib.parse.urlencode({"query": QUERY})
    req = urllib.request.Request(url, headers={
        "Accept": "text/csv",
        "User-Agent": "flightportrait-network/1 (https://flightportrait.com)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        rows = csv.DictReader(io.TextIOWrapper(resp, encoding="utf-8"))
        return {r["iata"]: float(r["pax"]) for r in rows if r["pax"]}


def main() -> None:
    with open(AIRPORTS) as fh:
        airports = json.load(fh)
    with open(RUNWAYS) as fh:
        runways = json.load(fh)
    pax = {} if "--offline" in sys.argv else fetch_pax()

    # every field is keyed under both its codes; group them by position
    fields = {}
    for code, a in airports.items():
        fields.setdefault((a[1], a[2]), []).append(code)

    rows = []
    for pos, codes in fields.items():
        a = airports[codes[0]]
        tier = a[3] if len(a) > 3 else 1
        rws, known = [], None
        for code in codes:
            if code in runways and not rws:
                rws = runways[code]
            if code in pax and known is None:
                known = pax[code]
        longest = max([r[4] or 0 for r in rws] or [0])
        # length carries most of the signal; a second or third runway
        # of the same class is a busy field, not a longer one
        strip = longest * math.sqrt(len(rws)) if rws else 0
        rows.append([pos, tier, strip, known])

    # passengers per unit of runway among fields that have both, by
    # class: the scale for guessing the rest, halved so a guess never
    # outranks a surveyed peer of the same size
    scale = {}
    for tier in (1, 2):
        ratios = sorted(r[3] / r[2] for r in rows
                        if r[1] == tier and r[2] and r[3])
        scale[tier] = ratios[len(ratios) // 2] / 2 if ratios else 0
    for r in rows:
        if r[3] is None:
            r[3] = r[2] * scale[r[1]]
    rows.sort(key=lambda r: (-r[3], -r[1], -r[2]))

    for rank, (pos, _, _, _) in enumerate(rows, 1):
        for code in fields[pos]:
            a = airports[code]
            entry = a[:4] + [1] * (4 - len(a[:4]))
            entry.append(rank)
            airports[code] = entry

    with open(AIRPORTS, "w") as fh:
        json.dump(airports, fh, separators=(",", ":"), sort_keys=True)
    guessed = sum(1 for r in rows if r[0] and not any(
        c in pax for c in fields[r[0]]))
    print("ranked %d fields (%d codes), %d from passengers, %d guessed; "
          "top: %s" % (len(fields), len(airports), len(fields) - guessed,
                       guessed, ", ".join(fields[r[0]][0] for r in rows[:12])))


if __name__ == "__main__":
    main()
