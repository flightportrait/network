"""The pushed sky: lines merge, ages tick, silence expires, the poll
steps aside while lines flow."""
import asyncio
import json
import time

from app.livesky import LiveSky, read_lines, FRESH_S
from app.snapshot import AIRCRAFT_FIELDS


def test_lines_merge_and_age():
    live = LiveSky(max_aircraft=10)
    t0 = 1000.0
    assert live.ingest({"hex": "aaaaaa", "flight": "SIA1  ", "lat": 1.3,
                        "lon": 103.8, "seen": 0.2, "seen_pos": 0.2,
                        "rssi": -9.0}, t0)
    assert not live.ingest({"flight": "nohex"}, t0)
    assert live.ingest({"hex": "aaaaaa", "lat": 1.31, "lon": 103.81,
                        "seen": 0.0, "seen_pos": 0.0}, t0 + 3)
    snap = live.snapshot(t0 + 5)
    assert snap.aircraft_count == 1 and snap.with_pos_count == 1
    a = snap.aircraft[0]
    assert set(a) <= set(AIRCRAFT_FIELDS)
    assert a["lat"] == 1.31 and "flight" not in a       # the last line wins whole
    assert a["seen"] == 2.0 and a["seen_pos"] == 2.0     # aged to now
    assert snap.generated_at == t0 + 3
    assert live.fresh(t0 + 3 + FRESH_S) and not live.fresh(t0 + 3 + FRESH_S + 1)
    # silence: the aircraft expires
    assert live.snapshot(t0 + 3 + 61).aircraft_count == 0


def test_reads_readsb_lines_over_tcp():
    async def run():
        lines = [json.dumps({"hex": "bbbbbb", "lat": 46.2, "lon": 6.1,
                             "seen": 0.1}) + "\n",
                 "not json\n",
                 json.dumps({"hex": "cccccc", "seen": 0.4}) + "\n"]

        async def serve(reader, writer):
            for line in lines:
                writer.write(line.encode())
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        live = LiveSky(max_aircraft=10)
        task = asyncio.create_task(read_lines(live, "127.0.0.1", port))
        for _ in range(50):
            await asyncio.sleep(0.05)
            if live.version >= 2:
                break
        task.cancel()
        server.close()
        snap = live.snapshot(time.time())
        return sorted(a["hex"] for a in snap.aircraft), snap.with_pos_count
    hexes, with_pos = asyncio.run(run())
    assert hexes == ["bbbbbb", "cccccc"] and with_pos == 1


def test_poll_steps_aside_while_lines_flow(ctx):
    from app.poller import poll_snapshot_once
    client, app, sm, settings, readsb = ctx
    live = app.state.live
    assert live is not None
    before = app.state.snapshot
    live.ingest({"hex": "dddddd", "seen": 0.0}, time.time())
    asyncio.run(poll_snapshot_once(app))
    assert app.state.snapshot is before                  # untouched
    live.last_line_at = time.time() - FRESH_S - 5
    asyncio.run(poll_snapshot_once(app))
    assert app.state.snapshot is not before               # the poll is back
