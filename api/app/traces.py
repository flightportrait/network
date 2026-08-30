"""Rolling position history, built from the snapshots we already poll.

readsb keeps its own trace files, but their layout is a readsb-internal
detail; the API owns its trails instead, fed by the same 5 s poll that
serves /v1/aircraft. In-memory only: a restart forgets the sky's last
half hour, and the trails regrow within minutes. That trade keeps the
whole feature dependency-free and the process disposable.
"""
import threading
import time
from collections import deque


class TraceBook:
    """Thread-safe: the poller writes from the event loop while sync route
    handlers read from the threadpool, so every access takes the lock."""

    def __init__(self, retention_s: int = 1800, max_points: int = 720):
        self.retention_s = retention_s
        self.max_points = max_points
        self._traces: dict[str, deque] = {}
        self._last_seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def record(self, snapshot) -> None:
        with self._lock:
            self._record(snapshot)

    def _record(self, snapshot) -> None:
        now = snapshot.generated_at or time.time()
        for entry in snapshot.aircraft:
            hex_id = entry.get("hex")
            if not hex_id or entry.get("lat") is None:
                continue
            trace = self._traces.setdefault(
                hex_id, deque(maxlen=self.max_points))
            point = [round(now, 1), entry["lat"], entry["lon"],
                     entry.get("alt_baro"), entry.get("track")]
            # the poll runs faster than aircraft report sometimes; skip
            # exact duplicates so a hovering point does not fill the buffer
            if trace and trace[-1][1] == point[1] and trace[-1][2] == point[2]:
                trace[-1][0] = point[0]
            else:
                trace.append(point)
            self._last_seen[hex_id] = now
        self._prune(now)

    def prune(self, now: float | None = None) -> None:
        with self._lock:
            self._prune(now)

    def _prune(self, now: float | None = None) -> None:
        now = now or time.time()
        dead = [h for h, seen in self._last_seen.items()
                if now - seen > self.retention_s]
        for hex_id in dead:
            self._traces.pop(hex_id, None)
            self._last_seen.pop(hex_id, None)

    def get(self, hex_id: str) -> list | None:
        with self._lock:
            trace = self._traces.get(hex_id.strip().lower())
            # copy the points too: the recorder mutates the last point's
            # timestamp in place, and the caller serializes outside the lock
            return [list(p) for p in trace] if trace else None
