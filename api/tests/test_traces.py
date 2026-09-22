"""The trace book and /v1/trace: recording, dedup, pruning, 404s."""
import asyncio
import time

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


def test_rejects_noncanonical_hex():
    book = TraceBook()
    book.record(_snap(10, [
        {"hex": "GARBAGE", "lat": 1.0, "lon": 2.0},   # not hex
        {"hex": "12345", "lat": 1.0, "lon": 2.0},     # too short
        {"hex": "ABCDEF", "lat": 1.0, "lon": 2.0},    # ok (upper)
    ]))
    assert book.get("garbage") is None
    assert book.get("12345") is None
    assert book.get("abcdef") is not None


def test_global_cardinality_cap_evicts_oldest():
    book = TraceBook(max_aircraft=3)
    for i, t in enumerate([10, 11, 12]):
        book.record(_snap(t, [{"hex": "%06x" % i, "lat": 1.0 + i,
                               "lon": 2.0}]))
    # a 4th distinct aircraft evicts the oldest-seen (000000)
    book.record(_snap(13, [{"hex": "000003", "lat": 9.0, "lon": 2.0}]))
    assert book.get("000000") is None
    assert book.get("000003") is not None
    assert len(book._traces) == 3


def test_prune_after_retention():
    book = TraceBook(retention_s=100)
    book.record(_snap(10, [{"hex": "aaa111", "lat": 1.0, "lon": 2.0}]))
    book.record(_snap(300, [{"hex": "bbb222", "lat": 3.0, "lon": 4.0}]))
    assert book.get("aaa111") is None            # gone after 290s of silence
    assert book.get("bbb222") is not None


def _trace(t0, pts):
    return {"timestamp": t0, "trace": [[dt, lat, lon, alt] for dt, lat, lon, alt in pts]}


def test_departure_from_the_ground_and_arrival():
    from app.departure import flight_bounds
    b = flight_bounds(_trace(1000.0, [
        (0, 1.35, 103.99, "ground"), (60, 1.36, 103.99, "ground"),
        (120, 1.37, 104.0, 800), (600, 1.6, 104.4, 12000),
        (1500, 2.3, 104.7, 20000), (2400, 2.8, 104.9, 8000),
        (3000, 3.0, 105.0, 3000), (3300, 3.1, 105.1, "ground"),
        (3360, 3.1, 105.1, "ground")]))
    assert b["departure"]["at"] == 1120.0 and b["departure"]["alt_ft"] == 800
    assert b["arrival"]["at"] == 4300.0


def test_first_heard_low_counts_first_heard_high_does_not():
    from app.departure import flight_bounds
    low = flight_bounds(_trace(1000.0, [(0, 1.37, 104.0, 900), (600, 1.6, 104.4, 12000)]))
    assert low["departure"]["at"] == 1000.0
    high = flight_bounds(_trace(1000.0, [(0, 2.0, 104.5, 36000), (600, 2.6, 105.0, 36000)]))
    assert high["departure"] is None and high["arrival"] is None


def test_an_earlier_flight_of_the_day_is_ignored():
    from app.departure import flight_bounds
    b = flight_bounds(_trace(1000.0, [
        (0, 1.37, 104.0, 600), (1200, 3.0, 105.0, "ground"),
        # two hours of silence, then the next sector
        (8400, 3.0, 105.0, "ground"), (8460, 3.05, 105.05, 700), (9000, 4.0, 106.0, 20000)]))
    assert b["departure"]["at"] == 9460.0 and b["arrival"] is None


def test_trace_carries_flight_bounds(ctx):
    client, app, sm, settings, readsb = ctx
    book = app.state.traces
    book.record(_snap(time.time(), [{"hex": "abc123", "lat": 1.5, "lon": 103.9,
                                     "alt_baro": 9000, "track": 80}]))
    readsb.trace_payload = _trace(time.time() - 600, [
        (0, 1.36, 103.99, "ground"), (90, 1.37, 104.0, 500), (600, 1.5, 103.9, 9000)])
    body = client.get("/v1/trace/abc123").json()
    assert body["departure"]["alt_ft"] == 500 and body["arrival"] is None
    readsb.trace_payload = Exception("upstream down")
    assert client.get("/v1/trace/abc123").json()["departure"]["alt_ft"] == 500  # cached a minute
