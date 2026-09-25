# parity

Checks that networkd answers what the Python service answers. Each
script takes the two base URLs, Python first, and reports every request
where the responses differ.

| script | what | how the two servers are fed |
|---|---|---|
| `live.py` | live routes, error envelopes, CORS (fixed cases) | both poll `netbench sky --frozen` |
| `stream.py` | first frames of `/v1/stream` | same (needs the `websockets` package) |
| `forwarded.py` | routes networkd forwards (`NETWORKD_FALLBACK`) | networkd in front of the Python service |
| `routes.py PATH...` | any GET paths, byte for byte, with timings | Python over Postgres, networkd over the snapshot `refdata_export` wrote from that same database |
| `classify.py PATH...` | for differing paths: order only, or real | same |
| `routes.py PATH...` for `/v1/routes`, `/v1/flights`, `/v1/airframes` | the same | both over one Postgres (the catalog and the airframe record change during the day, so networkd reads them there too), the snapshot, and the same routes, gaps and legs artifacts |
| `airports.py CODE...` | airport pages, allowing rows tied at the boards' 80-row cut (which Python itself returns differently from call to call) | same, plus the same `legs.db` and `boards.db` |

A new route switches over when `routes.py` shows it byte for byte, or
`classify.py` shows only orders Postgres leaves undefined; any such
order is written down in the route's module.

Known: `/v1/flights` for a callsign whose schedule rows tie on
`n_flights` (XFL167 on the 2026-09-24 copy): Postgres reads them with a
parallel scan, so Python itself returns either order from call to call;
networkd returns the heap order, one of the two.
