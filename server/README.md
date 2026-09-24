# server

The network server in Rust, and the harness that measures it.

- `crates/networkd`: one binary that reads the aggregator (readsb's JSON
  position port, with `aircraft.json` as the fallback) and serves the live
  API: `/v1/now`, `/v1/aircraft`, `/v2/point`, `/v1/trace`, `/v1/stream`.
  It reads the same `NETWORK_API_*` environment as the Python service in
  `../api`, and answers the same requests with the same bytes.
  `NETWORKD_BIND` sets the listen address (default `0.0.0.0:8092`).
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

`netbench sky --frozen` holds the sky still and adds edge-case aircraft,
so two servers polling it must answer byte for byte alike.

Opening thousands of sockets needs a raised open-file limit
(`ulimit -n`) on both ends.
