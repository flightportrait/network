"""The observed half of the airframe lifetime record: spells and events
from the network's own evidence.

Input is the history artifact (one entry per hex: registration and
operator spells, long silences, first and last observed). Everything
this module writes carries source "observed" and is replaced wholesale
by each import, in one transaction, so a reader never sees a half-built
history. Airframe rows persist across imports: their ids are the stable
handle other sources (registries, live events) attach to.

Events are worded as observations. "first_observed" is the network
noticing an airframe, not a delivery; "not_observed" is our silence, not
storage.
"""
import datetime
import gzip
import json

from sqlalchemy import delete, insert, select, update

from .refdata_models import Airframe, AirframeEvent, AirframeSpell

SOURCE = "observed"
CHUNK = 5000
# The archive starts mid-stream: an airframe seen in its first month was
# already flying, so a first sighting is an event only after this.
FIRST_OBSERVED_AFTER = datetime.timedelta(days=30)
# A truncated artifact must not wipe the history it would replace: once
# the record holds GUARD_FLOOR airframes, an artifact with fewer than
# MIN_KEEP_SHARE of them is refused.
GUARD_FLOOR = 1000
MIN_KEEP_SHARE = 0.8


def _day(value):
    return datetime.date.fromisoformat(value) if value else None


def _at(day):
    return datetime.datetime.combine(day, datetime.time(),
                                     tzinfo=datetime.timezone.utc)


def _events(airframe_id, entry, first_after):
    out = []
    first = _day(entry["first"])
    if first and first >= first_after:
        out.append(("first_observed", first, None))
    for kind, spells in (("registration_change", entry["regs"]),
                         ("operator_change", entry["ops"])):
        for before, after in zip(spells, spells[1:]):
            if _day(after[1]) > _day(before[2]):
                out.append((kind, _day(after[1]),
                            {"from": before[0], "to": after[0]}))
    for last_seen, seen_again in entry["gaps"]:
        a, b = _day(last_seen), _day(seen_again)
        out.append(("not_observed", a + datetime.timedelta(days=1),
                    {"last_seen": last_seen, "seen_again": seen_again,
                     "days": (b - a).days - 1}))
    seen, rows = set(), []
    for kind, day, detail in out:
        if (kind, day) in seen:
            continue
        seen.add((kind, day))
        rows.append({"airframe_id": airframe_id, "kind": kind,
                     "at": _at(day), "detail": detail, "source": SOURCE,
                     "visibility": "public"})
    return rows


def _spells(airframe_id, hex_id, entry):
    rows = [{"airframe_id": airframe_id, "kind": "hex", "value": hex_id,
             "first_date": _day(entry["first"]),
             "last_date": _day(entry["last"]), "n_obs": entry["n"],
             "source": SOURCE}]
    for kind, spells in (("registration", entry["regs"]),
                         ("operator", entry["ops"])):
        for value, first, last, n in spells:
            rows.append({"airframe_id": airframe_id, "kind": kind,
                         "value": value[:16], "first_date": _day(first),
                         "last_date": _day(last), "n_obs": n,
                         "source": SOURCE})
    return rows


def _insert(session, model, rows):
    for start in range(0, len(rows), CHUNK):
        session.execute(insert(model), rows[start:start + CHUNK])


def ingest_history(session, path):
    """Import the history artifact. Returns the number of airframes."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    frames = {h.lower(): e for h, e in doc["airframes"].items()
              if len(h) == 6}
    first_after = _day(doc["evidence_from"]) + FIRST_OBSERVED_AFTER

    by_hex, observed = {}, 0
    for hex_id, airframe_id, source in session.execute(
            select(AirframeSpell.value, AirframeSpell.airframe_id,
                   AirframeSpell.source)
            .where(AirframeSpell.kind == "hex")
            .order_by(AirframeSpell.first_date)):
        by_hex[hex_id] = airframe_id
        observed += source == SOURCE
    # Only what this import replaces counts: hex spells from registries or
    # the live watcher are not the artifact's to shrink.
    if (observed >= GUARD_FLOOR
            and len(frames) < MIN_KEEP_SHARE * observed):
        raise ValueError("history artifact has %d airframes, the record "
                         "%d: refusing to replace it" % (len(frames),
                                                         observed))

    # New hexes get an airframe each (AIRFRAMES.md D2: one per hex until
    # a serial number from a named source says two are one).
    new = [h for h in frames if h not in by_hex]
    for start in range(0, len(new), CHUNK):
        chunk = new[start:start + CHUNK]
        objs = [Airframe(type_code=frames[h]["type"]) for h in chunk]
        session.add_all(objs)
        session.flush()
        for hex_id, obj in zip(chunk, objs):
            by_hex[hex_id] = obj.id
        session.expunge_all()

    now = datetime.datetime.now(datetime.timezone.utc)
    updates = [{"id": by_hex[h], "first_observed": _day(e["first"]),
                "last_observed": _day(e["last"]), "updated_at": now}
               for h, e in frames.items()]
    for start in range(0, len(updates), CHUNK):
        session.execute(update(Airframe), updates[start:start + CHUNK])

    session.execute(delete(AirframeSpell)
                    .where(AirframeSpell.source == SOURCE))
    session.execute(delete(AirframeEvent)
                    .where(AirframeEvent.source == SOURCE))
    spells, events = [], []
    for hex_id, entry in frames.items():
        spells.extend(_spells(by_hex[hex_id], hex_id, entry))
        events.extend(_events(by_hex[hex_id], entry, first_after))
    _insert(session, AirframeSpell, spells)
    _insert(session, AirframeEvent, events)
    return len(frames)
