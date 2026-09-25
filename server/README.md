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
  `/v1/stations`. Turn the Python side off where these are on
  (`NETWORK_API_SQUAWK_WATCHER=off`, `NETWORK_API_STATION_POLLER=off`),
  so each is written once.
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
