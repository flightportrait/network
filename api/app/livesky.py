"""The live sky, pushed: readsb's JSON position port, one line per
position, merged into a state the sockets wake on.

The file poll (poller.py) is the fallback. While lines flow, the poll
steps aside and the published snapshot comes from here, rebuilt on
change at most a few times a second; `generated_at` is then the moment
the last position was heard, not the last file read.
"""
import asyncio
import json
import logging
import time

from .snapshot import AIRCRAFT_FIELDS, Snapshot

log = logging.getLogger("network-api.livesky")

FRESH_S = 10.0          # no line this long: the poll takes over
EXPIRE_S = 60.0         # not heard this long: gone
PUBLISH_MIN_S = 0.25    # rebuild the snapshot at most this often
TRACE_EVERY_S = 2.0     # trail points at the poll's old cadence


class LiveSky:
    def __init__(self, max_aircraft: int):
        self.max_aircraft = max_aircraft
        self.aircraft: dict[str, dict] = {}
        self.version = 0            # bumped per ingested line
        self.published = 0          # bumped per published snapshot
        self.last_line_at = 0.0     # wall clock of the last line
        self.connected = False
        self.cond = asyncio.Condition()

    # ---- state ---------------------------------------------------------
    def fresh(self, now: float | None = None) -> bool:
        now = now or time.time()
        return self.last_line_at > 0 and now - self.last_line_at <= FRESH_S

    def ingest(self, obj: dict, now: float) -> bool:
        if not isinstance(obj, dict):
            return False
        item = {k: obj[k] for k in AIRCRAFT_FIELDS if k in obj}
        hex_id = item.get("hex")
        if not isinstance(hex_id, str) or not hex_id:
            return False
        if isinstance(item.get("flight"), str):
            item["flight"] = item["flight"].strip()
        item["_at"] = now
        self.version += 1
        self.aircraft[hex_id] = item
        self.last_line_at = now
        return True

    def snapshot(self, now: float | None = None) -> Snapshot:
        """The state as an aircraft.json-shaped snapshot: seen and
        seen_pos aged to now, aircraft not heard for EXPIRE_S dropped."""
        now = now or time.time()
        out, with_pos = [], 0
        for hex_id in list(self.aircraft):
            item = self.aircraft[hex_id]
            age = now - item["_at"]
            if (item.get("seen") or 0) + age > EXPIRE_S:
                del self.aircraft[hex_id]
                continue
            entry = {k: v for k, v in item.items() if not k.startswith("_")}
            if "seen" in entry:
                entry["seen"] = round(entry["seen"] + age, 1)
            if "seen_pos" in entry:
                entry["seen_pos"] = round(entry["seen_pos"] + age, 1)
            if entry.get("lat") is not None and entry.get("lon") is not None:
                with_pos += 1
            out.append(entry)
            if len(out) >= self.max_aircraft:
                break
        return Snapshot(generated_at=self.last_line_at, aircraft=out,
                        aircraft_count=len(out), with_pos_count=with_pos)

    # ---- waking the sockets --------------------------------------------
    async def bump(self) -> int:
        async with self.cond:
            self.published += 1
            self.cond.notify_all()
            return self.published

    async def wait_change(self, seen: int) -> int:
        async with self.cond:
            await self.cond.wait_for(lambda: self.published > seen)
            return self.published


# ---- tasks -----------------------------------------------------------
async def read_lines(live: LiveSky, host: str, port: int) -> None:
    """Keep one connection to readsb's JSON port; merge every line."""
    backoff = 1.0
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10)
            live.connected = True
            backoff = 1.0
            log.info("live sky connected to %s:%s", host, port)
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    live.ingest(obj, time.time())
            finally:
                live.connected = False
                writer.close()
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log.warning("live sky %s:%s: %s", host, port, exc)
        log.info("live sky reconnecting in %.0f s", backoff)
        await asyncio.sleep(backoff)
        backoff = min(30.0, backoff * 2)


async def publish(app, live: LiveSky) -> None:
    """While lines flow: rebuild the snapshot on change, trail points
    every couple of seconds, wake the sockets."""
    seen_version = 0
    last_trace = 0.0
    while True:
        await asyncio.sleep(PUBLISH_MIN_S)
        if live.version == seen_version or not live.fresh():
            continue
        seen_version = live.version
        now = time.time()
        snap = live.snapshot(now)
        app.state.snapshot = snap
        if now - last_trace >= TRACE_EVERY_S:
            last_trace = now
            app.state.traces.record(snap)
        await live.bump()


def start(app, live: LiveSky, endpoint: str) -> list:
    host, _, port = endpoint.rpartition(":")
    return [asyncio.create_task(read_lines(live, host, int(port))),
            asyncio.create_task(publish(app, live))]
