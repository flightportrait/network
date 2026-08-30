"""The derived routes lookup.

The operator derives a callsign->route table nightly from openly
licensed trace archives and publishes it under ODbL. This service
consumes that artifact as a file — a gzipped JSON object mapping
callsign to [origin, dest] — handed over by an export job. A file
handoff, never a database link.

Missing file = feature dark (every lookup 404s); the map degrades to
showing no route line, nothing breaks.
"""
import gzip
import json
import os
import time


class RouteBook:
    def __init__(self, path: str, reload_s: float = 300.0):
        self._path = path
        self._reload_s = reload_s
        self._routes: dict[str, list] = {}
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
            self._routes = {}
            self._loaded_mtime = None
            return
        if mtime == self._loaded_mtime:
            return
        try:
            with gzip.open(self._path, "rt") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._routes = {k.strip().upper(): v
                                for k, v in data.items()}
                self._loaded_mtime = mtime
        except (OSError, ValueError):
            pass          # keep whatever loaded last; a bad file changes nothing

    def available(self) -> bool:
        """Whether the artifact is loaded (see LegBook.available)."""
        self._maybe_load()
        return self._loaded_mtime is not None

    def get(self, callsign: str) -> list | None:
        self._maybe_load()
        return self._routes.get(callsign.strip().upper())

    def count(self) -> int:
        self._maybe_load()
        return len(self._routes)
