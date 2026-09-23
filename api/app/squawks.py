"""Emergency squawks as airframe events (AIRFRAMES.md D6).

Every position line from our readsb passes through observe(). An
aircraft squawking 7500, 7600 or 7700, or reporting a readsb emergency
state, becomes an event only once the signal holds: at least MIN_REPORTS
reports spanning MIN_HOLD_S seconds. A single mis-decoded frame or a
pilot dialing through a code does not count. One event per episode; an
episode ends when the aircraft reports no signal for EPISODE_GAP_S.

7500 and readsb's "unlawful" are stored with visibility "held": served
nowhere until reviewed, because a mis-set 7500 published as a hijack is
harmful misinformation. Everything else is public and worded as what we
saw: "squawked 7700", never "emergency landing".

Only our own sky: an instance reading a remote aggregator (source mode
"point") records nothing.
"""
import asyncio
import datetime
import logging
import time

from sqlalchemy import select

from .refdata_models import Airframe, AirframeEvent, AirframeSpell

log = logging.getLogger("network-api.squawks")

SOURCE = "live"
CODES = {"7500", "7600", "7700"}
# readsb's emergency field; "none" and "reserved" are not signals.
EMERGENCIES = {"general", "lifeguard", "minfuel", "nordo", "unlawful",
               "downed"}
HELD = {"7500", "unlawful"}
MIN_REPORTS = 2
MIN_HOLD_S = 20.0
EPISODE_GAP_S = 300.0
FLUSH_S = 15.0


def signal_of(item):
    """(squawk code or None, emergency state or None), or None when the
    line carries no signal."""
    squawk = item.get("squawk")
    code = squawk if squawk in CODES else None
    emergency = item.get("emergency")
    state = emergency if emergency in EMERGENCIES else None
    if code is None and state is None:
        return None
    return code, state


class SquawkWatcher:
    def __init__(self):
        self._episodes: dict[str, dict] = {}
        self._pending: list[dict] = []

    def observe(self, item, now: float) -> None:
        hex_id = item.get("hex")
        if not isinstance(hex_id, str) or len(hex_id) != 6:
            return
        hex_id = hex_id.lower()
        sig = signal_of(item)
        ep = self._episodes.get(hex_id)
        if sig is None:
            return
        if ep is None or now - ep["last"] > EPISODE_GAP_S:
            ep = self._episodes[hex_id] = {
                "since": now, "last": now, "n": 0, "recorded": False,
                "code": sig[0], "state": sig[1]}
        ep["last"] = now
        ep["n"] += 1
        ep["code"] = ep["code"] or sig[0]
        ep["state"] = ep["state"] or sig[1]
        if (not ep["recorded"] and ep["n"] >= MIN_REPORTS
                and now - ep["since"] >= MIN_HOLD_S):
            ep["recorded"] = True
            held = ep["code"] in HELD or ep["state"] in HELD
            self._pending.append({
                "hex": hex_id, "at": ep["since"],
                "lat": item.get("lat"), "lon": item.get("lon"),
                "visibility": "held" if held else "public",
                "detail": {"code": ep["code"], "emergency": ep["state"],
                           "callsign": (item.get("flight") or "").strip()
                           or None,
                           "alt_baro": item.get("alt_baro")}})

    def sweep(self, now: float) -> None:
        """Forget episodes that ended."""
        for hex_id in [h for h, ep in self._episodes.items()
                       if now - ep["last"] > EPISODE_GAP_S]:
            del self._episodes[hex_id]

    def drain(self) -> list[dict]:
        out, self._pending = self._pending, []
        return out


def _airframe_for(session, hex_id, day):
    """The airframe the record holds for this hex; a new one (with a
    live hex spell) when the network has never recorded it."""
    airframe_id = session.execute(
        select(AirframeSpell.airframe_id)
        .where(AirframeSpell.kind == "hex", AirframeSpell.value == hex_id)
        .order_by(AirframeSpell.first_date.desc()).limit(1)).scalar()
    if airframe_id is not None:
        return airframe_id
    frame = Airframe(first_observed=day, last_observed=day)
    session.add(frame)
    session.flush()
    session.add(AirframeSpell(airframe_id=frame.id, kind="hex",
                              value=hex_id, first_date=day, last_date=day,
                              source=SOURCE))
    return frame.id


def write_events(sessionmaker, events) -> int:
    written = 0
    with sessionmaker() as session:
        for ev in events:
            at = datetime.datetime.fromtimestamp(ev["at"],
                                                 datetime.timezone.utc)
            airframe_id = _airframe_for(session, ev["hex"], at.date())
            exists = session.execute(
                select(AirframeEvent.id).where(
                    AirframeEvent.airframe_id == airframe_id,
                    AirframeEvent.kind == "squawk",
                    AirframeEvent.at == at,
                    AirframeEvent.source == SOURCE)).first()
            if exists:
                continue
            session.add(AirframeEvent(
                airframe_id=airframe_id, kind="squawk", at=at,
                lat=ev["lat"], lon=ev["lon"], detail=ev["detail"],
                source=SOURCE, visibility=ev["visibility"]))
            written += 1
        session.commit()
    return written


async def flush_loop(app, watcher: SquawkWatcher) -> None:
    """Write recorded episodes every FLUSH_S; a failed write keeps them
    for the next round."""
    while True:
        await asyncio.sleep(FLUSH_S)
        watcher.sweep(time.time())
        events = watcher.drain()
        if not events:
            continue
        try:
            n = await asyncio.to_thread(write_events,
                                        app.state.sessionmaker, events)
            if n:
                log.info("recorded %d squawk event(s)", n)
        except Exception as exc:          # noqa: BLE001 — never kill the loop
            log.warning("squawk write failed, retrying: %s", exc)
            watcher._pending[:0] = events
