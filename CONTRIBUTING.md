# Contributing

Issues and pull requests are welcome for the map, the API service,
and these docs.

- `web/` is static HTML, no build step. It talks to
  https://data.flightportrait.com unless `window.FP_API` is set.
  Do not add a compose file.
- `api/` is the service we run (see its README). Tests:
  `pip install -r api/requirements.txt pytest && python -m pytest
  api/tests/`.
- API shape: open an issue first. The v1 contract is frozen; stable
  operations only gain fields.
- `web/` is served at `/network/` on flightportrait.com.

CI runs the parse checks and the API tests on every push and PR.

Contributions are licensed under Apache-2.0.
