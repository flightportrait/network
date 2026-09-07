#!/usr/bin/env python3
"""Write web/assets/airlines.json: [icao, iata, name] per airline, from
the API's airline list, so the search box can answer airline names in
the browser with no round trip. Re-run when the reference tables change.

    python3 tools/airlines_asset.py
"""
import json
import os
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "web", "assets", "airlines.json")
API = os.environ.get("FP_API", "https://data.flightportrait.com")


def main() -> None:
    req = urllib.request.Request(API + "/v1/airlines", headers={
        "User-Agent": "flightportrait-network/1 (https://flightportrait.com)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        airlines = json.load(resp)["airlines"]
    rows = sorted(([a["icao"], a.get("iata") or "", a["name"], a.get("n_routes") or 0]
                   for a in airlines), key=lambda r: (-r[3], r[2]))
    with open(OUT, "w") as fh:
        json.dump(rows, fh, separators=(",", ":"), ensure_ascii=False)
    print(f"{len(rows)} airlines")


if __name__ == "__main__":
    main()
