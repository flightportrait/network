"""Test plumbing: SQLite in place of Postgres, a FakeReadsb in place of the
aggregator, pollers off, throttle state reset. Any attempt to reach a real
network is a test failure by construction (FakeReadsb is the only client)."""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import create_engine               # noqa: E402
from sqlalchemy.orm import sessionmaker as make_sm # noqa: E402
from sqlalchemy.pool import StaticPool             # noqa: E402

from app import ratelimit                          # noqa: E402
from app.db import Base                            # noqa: E402
from app.main import create_network_api_app        # noqa: E402
from app.settings import Settings                  # noqa: E402
from app.snapshot import build_snapshot            # noqa: E402


class FakeHTTPError(Exception):
    pass


class FakeReadsb:
    """Stands in for ReadsbClient. Payloads are plain dicts; setting one to
    an Exception makes that call fail. call_count proves throttle-before-
    proxy behavior."""

    def __init__(self):
        self.aircraft_payload = {"now": time.time(), "aircraft": []}
        self.clients_payload = {"now": time.time(), "clients": []}
        self.receivers_payload = {"now": time.time(), "receivers": []}
        self.circle_payload = {"now": time.time(), "aircraft": []}
        self.point_payload = {"now": time.time(), "aircraft": []}
        self.count_payload = 0
        self.calls = []

    async def _serve(self, name, payload):
        self.calls.append(name)
        if isinstance(payload, Exception):
            raise payload
        return payload

    async def aircraft(self):
        return await self._serve("aircraft", self.aircraft_payload)

    async def point_source(self, url_template, lat, lon, radius_nm):
        self.calls.append(("point_source", url_template, lat, lon,
                           radius_nm))
        if isinstance(self.point_payload, Exception):
            raise self.point_payload
        return self.point_payload

    async def clients(self):
        return await self._serve("clients", self.clients_payload)

    async def receivers(self):
        return await self._serve("receivers", self.receivers_payload)

    async def circle(self, lat, lon, radius_nm):
        self.calls.append(("circle", lat, lon, radius_nm))
        if isinstance(self.circle_payload, Exception):
            raise self.circle_payload
        return self.circle_payload

    async def count_for_station(self, half_id):
        return await self._serve(("count", half_id), self.count_payload)

    async def aclose(self):
        pass


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture
def ctx():
    ratelimit.reset()
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sm = make_sm(bind=engine, expire_on_commit=False)
    settings = Settings()
    readsb = FakeReadsb()
    app = create_network_api_app(settings=settings, sessionmaker=sm,
                                 readsb=readsb, start_pollers=False)
    # A fresh, current snapshot by default; staleness tests overwrite it.
    app.state.snapshot = build_snapshot(
        {"now": time.time(), "aircraft": []}, settings.max_aircraft)
    with TestClient(app) as client:
        yield client, app, sm, settings, readsb
