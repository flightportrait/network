# server

The network server in Rust, and the harness that measures it.

- `crates/networkd`: one binary that reads the aggregator (readsb's JSON
  position port, with `aircraft.json` as the fallback) and serves the live
  API: `/v1/now`, `/v1/aircraft`, `/v2/point`, `/v1/trace`, `/v1/stream`,
  and `/v2/stream`. It reads the same `NETWORK_API_*` environment as the
  Python service in `../api`, and answers the same requests with the same
  bytes. `/v2/stream` is `/v1/stream` sending only the fields that
  changed: an `upd` entry for an aircraft the client already holds
  carries `hex`, the changed fields (`null`: gone) and `seen`/`seen_pos`,
  to merge into the held object; a new aircraft arrives whole; a new box
  sends only what the client lacks. Same frames, same freshness, about a
  third of the bytes.
  `NETWORKD_BIND` sets the listen address (default `0.0.0.0:8092`).
  With `NETWORK_API_DATABASE_URL` set, it can also keep the shared record
  the Python service keeps: `NETWORKD_SQUAWKS=on` records emergency
  squawks as airframe events, `NETWORKD_STATIONS=on` keeps the stations
  registry (clients.json and receivers.json polls) and serves
  `/v1/stations`, `NETWORKD_ESTIMATES=on` runs the position estimator
  for aircraft that left coverage and serves `/v1/estimated` (its book
  kept in `live_state` across restarts), `NETWORKD_NAT=on` keeps the
  North Atlantic track messages. Turn the Python side off where these
  are on (`NETWORK_API_SQUAWK_WATCHER=off`,
  `NETWORK_API_STATION_POLLER=off`, `NETWORK_API_ESTIMATES=off`,
  `NETWORK_API_NAT_COLLECT=off`), so each is written once.
  Without `NETWORK_API_DATABASE_URL` (a self-hosted server, a Pi), the
  same switches keep the instance's own state in one SQLite file
  (`NETWORKD_STATE_PATH`, default `data/state.sqlite`): its feeders'
  registry, the squawks it heard, the estimate book, the NAT messages.
  The routes on data written during the day then read the snapshot's
  public copy of it (the community catalog, the answers on file, the
  airframe record), a day old, plus the instance's own squawks.
  `NETWORKD_FLEET_BIND` (e.g. `0.0.0.0:8093`; unset: off) opens a
  second, private listener, the fleet tier, for one known client (the
  frames' backend). It is not part of the public API, is not in
  `/openapi.json`, and the public listener answers 404 for all of it.
  Every `/fleet/v1/*` request carries
  `Authorization: Bearer <NETWORKD_FLEET_TOKEN>` (at least 32
  characters, or the tier stays off); a missing or wrong token is 401
  `{"error": "unauthorized", "detail": ...}`. No rate limits, no CORS,
  every response `Cache-Control: no-store`, errors in the public shape.
  - `GET /fleet/v1/point/{lat}/{lon}/{radius_nm}`: the `/v2/point`
    envelope (`ac`, `msg`, `now`, `total`, `ctime`, `ptime`), nearest
    first, `dst` in nm, radius capped at 250 nm, aircraft on the ground
    included.
  - `GET /fleet/v1/callsign/{callsign}`: `{"ac": [...], "now": ...}`,
    the aircraft live under that callsign (trimmed, any case); `ac` is
    empty when there are none.
  - `GET /fleet/v1/routes?cs=A,B`: the `/v1/routes` body, up to 200
    callsigns.
  - `GET /fleet/healthz` (no token): `ok`, `age_s`, `generated_at`,
    `aircraft`; 503 `stale_snapshot` past `NETWORK_API_STALE_AFTER_S`.

  Aircraft carry the public fields and, each omitted when unknown:
  `mil` (bit 0 of readsb's `dbFlags`, or the first digit of the
  registry's tar1090-db flags), `type_name` and `class` (the type's name
  and category from `ref_types`), `operator` and `operator_icao` (as
  `/v1/airframes/{hex}` resolves them), `year`, `route` (`[org, ...via,
  dst]`, as `/v1/routes`) and `source` (readsb's message type collapsed
  to `adsb`, `mlat`, `tisb`, `adsr`, `adsc`, `mode_s` or `other`). The
  enrichment reads memory and the local reference snapshot; the
  community catalog is asked at most once per request, for callsigns it
  has not answered in ten minutes.
  `/openapi.json` and the docs pages are copies in
  `crates/networkd/static`; after an API change, rerun
  `python api/export_openapi.py --networkd server/crates/networkd/static`
  (a test fails until they match).
- `crates/netbench`: `sky` is a synthetic readsb (moving aircraft on the
  JSON port and in `aircraft.json`); `viewers` is a crowd of map clients
  on the live stream, reporting frame age, egress, and the server's CPU
  and memory.

The Python service stays the reference until every route is ported.

## Build and test

```sh
cargo build --release
cargo test --release
```

## Measure

```sh
target/release/netbench sky --aircraft 10000 &
NETWORK_API_UPSTREAM=http://127.0.0.1:8090 \
NETWORK_API_LIVE_JSON=127.0.0.1:30047 \
NETWORK_API_STREAM_MAX_PER_IP=100000 \
  target/release/networkd &
target/release/netbench viewers --url ws://127.0.0.1:8092/v1/stream \
  --count 1000 --pid $! --out report.json
```

`netbench sky --interval 0.5` reports each aircraft twice a second
(readsb's `--net-json-port-interval`). `netbench sky --frozen` holds the sky still and adds edge-case aircraft,
so two servers polling it must answer byte for byte alike.

Opening thousands of sockets needs a raised open-file limit
(`ulimit -n`) on both ends.
