"""The North Atlantic Organised Track System, read from the track message.

Twice a day the oceanic control centres publish the day's tracks across
the North Atlantic as a NOTAM: Shanwick (EGGX) the westbound daytime
set, Gander (CZQX) the eastbound night set. The FAA's North Atlantic
Tracks page serves the current message as JSON (NAT_URL), one entry per
part of the message. A track reads

    C MALOT 54/20 56/30 5630/40 55/50 LOMSI
    EAST LVLS NIL
    WEST LVLS 340 350 360 370 380 390 400

a letter, the oceanic entry point (a named fix), one latitude/longitude
point per 10 degrees of longitude (54/20 is 54N 20W, 5630/40 is 56°30'N
40W; always west longitude), the exit point, and the flight levels
allowed in each direction (NIL: none).

The parser is pure; fetch() and the fix table (nat_fixes.csv, read
once) are the only I/O. The estimator uses the
tracks to fly a lost aircraft along its lane instead of the great
circle (docs: estimates.md).
"""
import csv
import datetime
import json
import os
import re
import urllib.request

NAT_URL = "https://nms.aim.faa.gov/datanat/nat.json"

_POINT = re.compile(r"^(\d{2})(\d{2})?/(\d{2,3})$")
_TRACK = re.compile(r"^([A-Z])\s+(.+)$")
_LEVELS = re.compile(r"^(EAST|WEST)\s+LVLS\s+(.*)$")


def parse_point(token):
    """'54/20' -> (54.0, -20.0); '5630/40' -> (56.5, -40.0); else None."""
    m = _POINT.match(token)
    if not m:
        return None
    lat = int(m.group(1)) + (int(m.group(2)) / 60.0 if m.group(2) else 0.0)
    lon = -float(int(m.group(3)))
    if not (40 <= lat <= 80 and -80 <= lon <= 0):
        return None
    return lat, lon


def _levels(text):
    text = text.strip()
    if not text or text.startswith("NIL"):
        return []
    return [int(t) for t in text.split() if t.isdigit()]


def parse_parts(parts):
    """The FAA JSON (a list of message parts) -> list of messages:
    {issuer, tmi, valid_from, valid_to, tracks: [{letter, entry, exit,
    points: [[lat, lon], ...], east_levels, west_levels, direction}]}.
    Parts of one message share issuer and validity."""
    by_msg = {}
    for p in parts or []:
        key = (p.get("icao_id"), p.get("start_datetime"), p.get("end_datetime"))
        msg = by_msg.setdefault(key, {
            "issuer": p.get("icao_id"), "valid_from": p.get("start_datetime"),
            "valid_to": p.get("end_datetime"), "tmi": None, "tracks": [],
            "raw": "", "_parts": []})
        msg["_parts"].append((p.get("part_no") or 0, p.get("condition_message") or ""))
    out = []
    for msg in by_msg.values():
        text = "\n".join(t for _, t in sorted(msg.pop("_parts")))
        msg["raw"] = text.replace("\r", "")
        current = None
        for raw in text.replace("\r", "").split("\n"):
            line = raw.strip().rstrip("-").strip()
            tmi = re.search(r"TMI IS (\d+)", line)
            if tmi:
                msg["tmi"] = int(tmi.group(1))
            lv = _LEVELS.match(line)
            if lv and current is not None:
                current["east_levels" if lv.group(1) == "EAST" else
                        "west_levels"] = _levels(lv.group(2))
                continue
            m = _TRACK.match(line)
            if not m:
                continue
            tokens = m.group(2).split()
            points = [parse_point(t) for t in tokens]
            coords = [p for p in points if p]
            if len(coords) < 2:
                continue                  # not a track line
            named = [t for t, p in zip(tokens, points) if p is None]
            current = {"letter": m.group(1),
                       "entry": tokens[0] if points[0] is None else None,
                       "exit": tokens[-1] if points[-1] is None else None,
                       "points": [list(c) for c in coords],
                       "east_levels": [], "west_levels": [],
                       "named": named}
            msg["tracks"].append(current)
        for t in msg["tracks"]:
            t["direction"] = ("W" if t["west_levels"] and not t["east_levels"]
                              else "E" if t["east_levels"] and not t["west_levels"]
                              else None)
            t.pop("named", None)
        if msg["tracks"]:
            out.append(msg)
    return out


def fetch(url=NAT_URL, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": "flightportrait-network (+https://flightportrait.com/network/)",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return parse_parts(json.loads(r.read()))


# ---- the named ends: entry and exit fixes --------------------------------
FIXES_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "refdata", "nat_fixes.csv")
_FIXES = {}


def fixes():
    """{name: (lat, lon)} for the named points around the ocean, from
    refdata/nat_fixes.csv (tools/nat_fixes.py: FAA NASR, UK and Irish AIPs)."""
    if not _FIXES:
        with open(FIXES_CSV, newline="") as fh:
            for row in csv.DictReader(fh):
                _FIXES[row["name"]] = (float(row["lat"]), float(row["lon"]))
    return _FIXES


def track_path(track):
    """A track's whole path, entry fix, oceanic points, exit fix, in the
    message's order; a named end the table does not know is left off."""
    known = fixes()
    path = [tuple(p) for p in track["points"]]
    if track.get("entry") in known:
        path.insert(0, known[track["entry"]])
    if track.get("exit") in known:
        path.append(known[track["exit"]])
    return path


def unknown_fixes(messages):
    """Named ends the table cannot place (a new fix: rerun the tool)."""
    known = fixes()
    return sorted({n for m in messages for t in m["tracks"]
                   for n in (t.get("entry"), t.get("exit"))
                   if n and n not in known})


def active(messages, when):
    """The tracks valid at `when` (aware datetime), each with its
    message's issuer and validity."""
    out = []
    for msg in messages:
        try:
            a = datetime.datetime.fromisoformat(msg["valid_from"].replace("Z", "+00:00"))
            b = datetime.datetime.fromisoformat(msg["valid_to"].replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            continue
        if a <= when <= b:
            for t in msg["tracks"]:
                out.append(dict(t, issuer=msg["issuer"], tmi=msg["tmi"],
                                valid_from=msg["valid_from"],
                                valid_to=msg["valid_to"]))
    return out


# ---- the collector: every message kept, from the day it runs ----------
FETCH_S = 1800


def _when(iso):
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))


def store(sessionmaker, messages):
    """Keep each message once (issuer + start of validity). Returns how
    many were new."""
    from sqlalchemy import select
    from .refdata_models import NatMessage
    new = 0
    with sessionmaker() as session:
        for msg in messages:
            try:
                a, b = _when(msg["valid_from"]), _when(msg["valid_to"])
            except (AttributeError, KeyError, ValueError):
                continue
            seen = session.execute(select(NatMessage.id).where(
                NatMessage.issuer == msg["issuer"],
                NatMessage.valid_from == a)).first()
            if seen:
                continue
            session.add(NatMessage(issuer=msg["issuer"] or "?", tmi=msg["tmi"],
                                   valid_from=a, valid_to=b,
                                   tracks=msg["tracks"], raw=msg.get("raw")))
            new += 1
        session.commit()
    return new


async def collect(app):
    """Fetch the current message every FETCH_S and keep what is new."""
    import asyncio
    import logging
    log = logging.getLogger("network-api.nat")
    while True:
        try:
            msgs = await asyncio.to_thread(fetch)
            n = await asyncio.to_thread(store, app.state.sessionmaker, msgs)
            app.state.nat = msgs
            if n:
                log.info("nat: %d new track message(s)", n)
                missing = unknown_fixes(msgs)
                if missing:
                    log.warning("nat: fixes not in nat_fixes.csv: %s",
                                " ".join(missing))
        except asyncio.CancelledError:
            raise
        except Exception as exc:          # noqa: BLE001 — next round
            log.warning("nat: fetch failed: %s", exc)
        await asyncio.sleep(FETCH_S)
