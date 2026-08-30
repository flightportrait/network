"""Per-airframe flight logs, read from an operator-supplied SQLite artifact.

The operator's pipeline observes flight legs nightly from openly
licensed trace archives and exports a window of them as an indexed
SQLite file — sixty days of global legs is
millions of rows, which is why this artifact is a database file rather
than the JSON-in-memory pattern the routes use. A copy of open data
handed over as a file, never a database link.

Missing file = feature dark (every lookup 404s); the airframe page says
"no log yet" and nothing breaks.
"""
import os
import sqlite3
import threading
import time


class LegBook:
    def __init__(self, path: str, reload_s: float = 300.0,
                 max_legs: int = 200):
        self._path = path
        self._reload_s = reload_s
        self._max_legs = max_legs
        self._conn: sqlite3.Connection | None = None
        self._loaded_mtime: float | None = None
        self._next_check = 0.0
        self._lock = threading.Lock()

    def _connect(self) -> None:
        now = time.monotonic()
        if now < self._next_check and self._conn is not None:
            return
        self._next_check = now + self._reload_s
        try:
            mtime = os.path.getmtime(self._path)
        except OSError:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            self._loaded_mtime = None
            return
        if mtime == self._loaded_mtime and self._conn is not None:
            return
        try:
            conn = sqlite3.connect(
                "file:%s?mode=ro" % self._path, uri=True,
                check_same_thread=False)
            conn.execute("SELECT 1 FROM legs LIMIT 1")
        except sqlite3.Error:
            return                      # keep whatever worked last
        if self._conn is not None:
            self._conn.close()
        self._conn = conn
        self._loaded_mtime = mtime

    def available(self) -> bool:
        """Whether the artifact is loaded. Routes use this to tell
        "not observed" (a fact about the world) from "feature dark"
        (a fact about our ops), which serve different status codes."""
        with self._lock:
            self._connect()
            return self._conn is not None

    def window_days(self) -> int | None:
        value = self.meta().get("window_days")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def get(self, hex_id: str) -> dict | None:
        with self._lock:
            self._connect()
            if self._conn is None:
                return None
            hex_id = hex_id.strip().lower()
            rows = self._conn.execute(
                "SELECT date, org, dst, dep_ts, arr_ts, max_alt, reg, type,"
                " callsign"
                " FROM legs WHERE hex = ?"
                " ORDER BY date DESC, dep_ts DESC LIMIT ?",
                (hex_id, self._max_legs)).fetchall()
        if not rows:
            return None
        legs = [{"date": r[0], "org": r[1], "dst": r[2],
                 "dep_ts": r[3], "arr_ts": r[4], "max_alt": r[5],
                 "callsign": r[8]}
                for r in rows]
        # identity from the freshest row that carries it
        reg = next((r[6] for r in rows if r[6]), None)
        type_code = next((r[7] for r in rows if r[7]), None)
        return {"hex": hex_id, "reg": reg, "type": type_code,
                "legs": legs}

    def airport(self, code: str) -> dict | None:
        """One airport's observed totals and busiest routes, derived on
        demand from the legs artifact. Circuits (org == dst) are
        excluded: a training loop is not a departure a visitor means.
        The departures BOARD is not derived here — it comes from the
        schedule table, the one place times are clustered properly in
        local time (routes_live joins the two)."""
        code = code.strip().upper()
        with self._lock:
            self._connect()
            if self._conn is None:
                return None
            q = self._conn.execute
            stats = q("SELECT COUNT(*), COUNT(DISTINCT dst),"
                      " COUNT(DISTINCT hex), COUNT(DISTINCT date)"
                      " FROM legs WHERE org = ?"
                      " AND (dst IS NULL OR dst <> org)", (code,)).fetchone()
            if not stats or not stats[0]:
                return None
            routes = q("SELECT dst, COUNT(*), COUNT(DISTINCT date)"
                       " FROM legs WHERE org = ? AND dst IS NOT NULL"
                       " AND dst <> org"
                       " GROUP BY dst ORDER BY 2 DESC LIMIT 15",
                       (code,)).fetchall()
        return {
            "code": code,
            "departures": stats[0], "destinations": stats[1],
            "tails": stats[2], "days_observed": stats[3],
            "routes": [{"dst": r[0], "flights": r[1], "days": r[2]}
                       for r in routes],
        }

    def flight(self, callsign: str) -> dict | None:
        """One flight number's observed life: the legs it flies, the
        tails that fly it, and its recent operations. Circuits and
        one-sided legs are excluded, as everywhere."""
        callsign = callsign.strip().upper()
        with self._lock:
            self._connect()
            if self._conn is None:
                return None
            q = self._conn.execute
            legs = q("SELECT org, dst, COUNT(*), COUNT(DISTINCT date),"
                     " MAX(date)"
                     " FROM legs WHERE callsign = ? AND org IS NOT NULL"
                     " AND dst IS NOT NULL AND org <> dst"
                     " GROUP BY org, dst ORDER BY 3 DESC LIMIT 10",
                     (callsign,)).fetchall()
            if not legs:
                return None
            # same valid-leg predicate as legs/recent, so a tail's count
            # is over the flights that actually count (not circuits or
            # one-sided sightings) — the docstring's promise, in SQL
            tails = q("SELECT hex, reg, type, COUNT(*)"
                      " FROM legs WHERE callsign = ? AND hex IS NOT NULL"
                      " AND org IS NOT NULL AND dst IS NOT NULL AND org <> dst"
                      " GROUP BY hex ORDER BY 4 DESC LIMIT 8",
                      (callsign,)).fetchall()
            recent = q("SELECT date, org, dst, dep_ts, arr_ts"
                       " FROM legs WHERE callsign = ? AND org IS NOT NULL"
                       " AND dst IS NOT NULL AND org <> dst"
                       " ORDER BY date DESC, dep_ts DESC LIMIT 10",
                       (callsign,)).fetchall()
        return {
            "callsign": callsign,
            "legs": [{"org": r[0], "dst": r[1], "flights": r[2],
                      "days": r[3], "last": r[4]} for r in legs],
            "aircraft": [{"hex": r[0], "reg": r[1], "type": r[2],
                          "flights": r[3]} for r in tails],
            "recent": [{"date": r[0], "org": r[1], "dst": r[2],
                        "dep_ts": r[3], "arr_ts": r[4]} for r in recent],
        }

    def meta(self) -> dict:
        with self._lock:
            self._connect()
            if self._conn is None:
                return {}
            try:
                rows = dict(self._conn.execute(
                    "SELECT key, value FROM meta").fetchall())
            except sqlite3.Error:
                return {}
        return rows
