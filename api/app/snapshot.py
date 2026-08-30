"""The in-memory sky snapshot served by /v1/now and /v1/aircraft.

One background poll produces one Snapshot; routes only ever read memory.
That keeps the load on readsb constant (~0.2 req/s) no matter what the
public does — a cache-miss stampede lands on this object, never on the
aggregator itself.
"""
import time
from dataclasses import dataclass, field

# Passthrough allowlist. Everything readsb knows beyond these fields stays
# private by default; widening this list is an API change, not an accident.
AIRCRAFT_FIELDS = (
    "hex", "flight", "t", "r", "lat", "lon", "alt_baro", "gs",
    "track", "category", "squawk", "seen", "seen_pos",
)


@dataclass
class Snapshot:
    generated_at: float = 0.0
    aircraft: list = field(default_factory=list)
    aircraft_count: int = 0
    with_pos_count: int = 0

    def age_s(self) -> float:
        return time.time() - self.generated_at

    def fresh(self, stale_after_s: int) -> bool:
        return self.generated_at > 0 and self.age_s() <= stale_after_s


def build_snapshot(raw: dict, max_aircraft: int) -> Snapshot:
    aircraft = []
    with_pos = 0
    for entry in (raw.get("aircraft") or [])[:max_aircraft]:
        if not isinstance(entry, dict):
            continue
        item = {k: entry[k] for k in AIRCRAFT_FIELDS if k in entry}
        if isinstance(item.get("flight"), str):
            item["flight"] = item["flight"].strip()
        if item.get("lat") is not None and item.get("lon") is not None:
            with_pos += 1
        aircraft.append(item)
    return Snapshot(
        generated_at=float(raw.get("now") or time.time()),
        aircraft=aircraft,
        aircraft_count=len(aircraft),
        with_pos_count=with_pos,
    )
