"""The trace book and /v1/trace: recording, dedup, pruning, 404s."""
import asyncio

from app.poller import poll_snapshot_once
from app.snapshot import build_snapshot
from app.traces import TraceBook


def _snap(now, aircraft):
    return build_snapshot({"now": now, "aircraft": aircraft}, 1000)


def test_records_and_serves(ctx):
    client, app, sm, settings, readsb = ctx
    readsb.aircraft_payload = {"now": 1000.0, "aircraft": [
        {"hex": "76cd06", "lat": 1.5, "lon": 103.8, "alt_baro": 6000,
         "track": 85.0},
    ]}
    asyncio.new_event_loop().run_until_complete(poll_snapshot_once(app))
    body = client.get("/v1/trace/76cd06").json()
    assert body["hex"] == "76cd06"
    assert body["points"][0][1:3] == [1.5, 103.8]


def test_trace_unknown_404_no_store(ctx):
    client, app, sm, settings, readsb = ctx
    response = client.get("/v1/trace/abc123")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_dedup_and_growth():
    book = TraceBook()
    book.record(_snap(10, [{"hex": "aaa111", "lat": 1.0, "lon": 2.0}]))
    book.record(_snap(15, [{"hex": "aaa111", "lat": 1.0, "lon": 2.0}]))
    assert len(book.get("aaa111")) == 1          # duplicate position merged
    assert book.get("aaa111")[0][0] == 15        # but timestamp advanced
    book.record(_snap(20, [{"hex": "aaa111", "lat": 1.1, "lon": 2.0}]))
    assert len(book.get("aaa111")) == 2


def test_positionless_not_recorded():
    book = TraceBook()
    book.record(_snap(10, [{"hex": "aaa111"}]))
    assert book.get("aaa111") is None


def test_prune_after_retention():
    book = TraceBook(retention_s=100)
    book.record(_snap(10, [{"hex": "aaa111", "lat": 1.0, "lon": 2.0}]))
    book.record(_snap(300, [{"hex": "bbb222", "lat": 3.0, "lon": 4.0}]))
    assert book.get("aaa111") is None            # gone after 290s of silence
    assert book.get("bbb222") is not None
