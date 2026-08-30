# The network API

The service behind [data.flightportrait.com](https://data.flightportrait.com):
FastAPI next to a readsb aggregator, an in-memory sky snapshot, a
stations registry in Postgres, history served from operator-supplied
artifact files.

It is published so [docs/privacy.md](../docs/privacy.md) is auditable
in code, not just prose: the full station UUID is never stored
(`app/stations.py`, `app/models.py` — sha256 verifier and half id
only), feeder IPs are discarded at the parse boundary, public
locations are coarse midpoints of coverage, never feeder-typed
coordinates.

This is the code we run, not a product:

- There is one network. We operate the instance; the map, the API,
  and the data are what is open.
- No deployment support and no compose here. A fresh checkout serves
  an empty sky and 503s for history until artifacts exist.
- The v1 contract is frozen: operations marked `stable` in the spec
  only ever gain fields. For API shape changes, open an issue first.

Tests: `pip install -r api/requirements.txt pytest && python -m pytest
api/tests/` — SQLite and a fake readsb stand in for the real thing; no
network, no Docker. The spec exports with `python api/export_openapi.py`.
