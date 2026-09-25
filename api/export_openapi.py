"""Dump the OpenAPI document.

Usage:
    python export_openapi.py [out.json]
    python export_openapi.py --networkd DIR

No argument prints to stdout. The published docs
(github.com/flightportrait/docs) carry the exported snapshot.
--networkd writes the document and the three docs pages (Swagger UI,
its OAuth2 redirect, ReDoc) as the app serves them, for networkd to
serve as they are (tests/test_openapi_static.py holds them equal).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import create_network_api_app


def build_spec() -> dict:
    engine = create_engine("sqlite://")
    app = create_network_api_app(
        sessionmaker=sessionmaker(bind=engine), start_pollers=False)
    return app.openapi()


PAGES = {"docs.html": "/docs", "oauth2-redirect.html": "/docs/oauth2-redirect",
         "redoc.html": "/redoc"}


def served() -> dict:
    """{file name: bytes} of the docs pages, as the app serves them."""
    from fastapi.testclient import TestClient
    engine = create_engine("sqlite://")
    app = create_network_api_app(
        sessionmaker=sessionmaker(bind=engine), start_pollers=False)
    client = TestClient(app)
    return {name: client.get(path).content for name, path in PAGES.items()}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--networkd":
        out = argv[1]
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "openapi.json"), "w") as fh:
            fh.write(json.dumps(build_spec(), indent=2) + "\n")
        for name, body in served().items():
            with open(os.path.join(out, name), "wb") as fh:
                fh.write(body)
        print("wrote", out, file=sys.stderr)
        return 0
    text = json.dumps(build_spec(), indent=2) + "\n"
    if not argv or argv[0] == "-":
        sys.stdout.write(text)
        return 0
    path = argv[0]
    with open(path, "w") as fh:
        fh.write(text)
    print("wrote", path, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
