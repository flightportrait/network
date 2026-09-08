"""The gaps artifact: what observation alone could not answer.

gaps.json.gz, written by the operator's export beside routes.json.gz:
{callsign: {side, known, hint, n_recent, last_seen, last_lat, last_lon,
last_trk}}. side is the missing end; known is the settled one; the
last_* fields say where the truncated leg was last heard. Same
reload-on-mtime posture as RouteBook."""
import gzip
import json
import os
import time


class GapBook:
    def __init__(self, path: str, reload_s: float = 300.0):
        self._path = path
        self._reload_s = reload_s
        self._gaps: dict[str, dict] = {}
        self._ordered: list[tuple[str, dict]] = []
        self._loaded_mtime: float | None = None
        self._next_check = 0.0

    def _maybe_load(self) -> None:
        now = time.monotonic()
        if now < self._next_check:
            return
        self._next_check = now + self._reload_s
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            self._gaps, self._ordered = {}, []
            self._loaded_mtime = None
            return
        if mtime == self._loaded_mtime:
            return
        try:
            with gzip.open(self._path, "rt") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._gaps = {k.strip().upper(): v for k, v in data.items()
                              if isinstance(v, dict)}
                # Most-seen first: the answer worth the most sits on top.
                self._ordered = sorted(
                    self._gaps.items(),
                    key=lambda kv: (-int(kv[1].get("n_recent") or 0),
                                    kv[0]))
                self._loaded_mtime = mtime
        except (OSError, ValueError):
            pass

    def available(self) -> bool:
        self._maybe_load()
        return self._loaded_mtime is not None

    def get(self, callsign: str) -> dict | None:
        self._maybe_load()
        return self._gaps.get(callsign.strip().upper())

    def count(self) -> int:
        self._maybe_load()
        return len(self._gaps)

    def page(self, offset: int, limit: int, airline: str | None = None,
             side: str | None = None) -> tuple[list[tuple[str, dict]], int]:
        """(rows, total) for one page, optionally one airline prefix or
        one missing side."""
        self._maybe_load()
        rows = self._ordered
        if airline:
            rows = [kv for kv in rows if kv[0].startswith(airline)]
        if side:
            rows = [kv for kv in rows if kv[1].get("side") == side]
        return rows[offset:offset + limit], len(rows)
