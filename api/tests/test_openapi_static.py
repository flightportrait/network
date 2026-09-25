"""networkd serves the OpenAPI document and the docs pages from copies
committed beside it (server/crates/networkd/static). They must be what
this app serves; after an API change, rerun
    python export_openapi.py --networkd ../server/crates/networkd/static
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(HERE, "..", "server", "crates", "networkd", "static")
sys.path.insert(0, HERE)

import export_openapi  # noqa: E402


def test_networkd_serves_the_current_document():
    with open(os.path.join(STATIC, "openapi.json")) as fh:
        assert json.load(fh) == export_openapi.build_spec()


def test_networkd_serves_the_current_docs_pages():
    for name, body in export_openapi.served().items():
        with open(os.path.join(STATIC, name), "rb") as fh:
            assert fh.read() == body, name
