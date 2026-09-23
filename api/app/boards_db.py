"""The published boards artifact: what airports themselves publish
for today, one row per flight and direction (data/boards.db, merged
into the schedule nightly; this reader serves it as-is on the airport
page). Read-only, a fresh connection per call, absent artifact means
no board."""
import datetime
import os
import sqlite3
from zoneinfo import ZoneInfo


def _hhmm(minutes):
    return "%02d:%02d" % divmod(int(minutes), 60) if minutes is not None else None


class BoardBook:
    def __init__(self, path: str):
        self.path = path

    def available(self) -> bool:
        return bool(self.path) and os.path.exists(self.path)

    def today(self, iata: str, tz: str | None):
        """{"day", "departures": [{flight, dst, dep}], "arrivals": [{flight,
        org, arr}]} for the airport's local day, or None."""
        if not self.available() or not iata:
            return None
        try:
            zone = ZoneInfo(tz) if tz else datetime.timezone.utc
        except (KeyError, ValueError):
            zone = datetime.timezone.utc
        day = datetime.datetime.now(zone).date().isoformat()
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=5)
            try:
                rows = conn.execute(
                    "SELECT kind, flight, counterpart, sched_min FROM boards"
                    " WHERE airport = ? AND day = ? AND sched_min IS NOT NULL"
                    " ORDER BY sched_min, flight", (iata, day)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        if not rows:
            return None
        deps, arrs, seen = [], [], set()
        for kind, flight, cp, sched in rows:
            if (kind, flight) in seen:
                continue
            seen.add((kind, flight))
            if kind == "dep":
                deps.append({"flight": flight, "dst": cp, "dep": _hhmm(sched)})
            else:
                arrs.append({"flight": flight, "org": cp, "arr": _hhmm(sched)})
        return {"day": day, "departures": deps, "arrivals": arrs}
