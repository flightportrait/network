"""Dump the OpenAPI document.

Usage:
    python export_openapi.py [out.json]

No argument prints to stdout. The published docs
(github.com/flightportrait/docs) carry the exported snapshot.
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


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
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
