"""Transport Canada CADORS occurrences as airframe events.

The Civil Aviation Daily Occurrence Reporting System: initial reports of
occurrences involving Canadian-registered aircraft, and any aircraft at
Canadian aerodromes or in airspace Canada controls. Published on the
Government of Canada open data portal under the Open Government Licence
- Canada; attribution: "Contains information licensed under the Open
Government Licence - Canada."

Input: a directory holding the portal's CADORS_Occurrence_Information,
CADORS_Occurrence_Event_Information and CADORS_Aircraft_Information
CSVs as downloaded.

Matching is the whole risk. Registrations are reissued: C-GJZT in 2011
may be another aircraft than C-GJZT today. An occurrence attaches to an
airframe only when its date falls inside a registration spell the
record holds for that airframe (with SLACK days either side), so its
reach grows with the network's own evidence rather than guessing
backwards. Owner and operator names are not imported (some are private
people); the report number is the evidence.

CADORS is an initial report, not an investigation finding; the event
detail keeps its own words (incident or accident, the event categories,
damage, phase of flight) and nothing more.
"""
import csv
import datetime
import os

from sqlalchemy import select

from .refdata_models import AirframeEvent, AirframeSpell

SOURCE = "cadors"
LICENCE = "OGL-Canada"
SLACK = datetime.timedelta(days=30)
# Older reports cannot meet a spell: the record starts 2025-08-28.
MIN_DATE = datetime.date(2025, 7, 1)
CHUNK = 5000


def _read(directory, name):
    path = os.path.join(directory, name)
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        yield from csv.DictReader(fh)


def _num(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _norm(reg):
    return (reg or "").replace("-", "").replace(" ", "").upper()


def _registration(row):
    """CADORS writes Canadian marks without the nationality prefix
    (GJZT for C-GJZT) and foreign ones without hyphens (A6EDA)."""
    domestic = (row.get("aircraftregistration") or "").strip().upper()
    if domestic:
        return domestic if domestic.startswith("C-") else "C-" + domestic
    return (row.get("foreignaircraftregistration") or "").strip().upper()


def _when(date_s, time_s):
    try:
        day = datetime.date.fromisoformat((date_s or "")[:10])
    except ValueError:
        return None, None
    hhmm = "".join(c for c in (time_s or "") if c.isdigit())[:4]
    t = datetime.time()
    if len(hhmm) == 4 and int(hhmm[:2]) < 24 and int(hhmm[2:]) < 60:
        t = datetime.time(int(hhmm[:2]), int(hhmm[2:]))
    return day, datetime.datetime.combine(day, t,
                                          tzinfo=datetime.timezone.utc)


def parse(directory, min_date=MIN_DATE):
    """Yield (registration, at, detail) per aircraft involved in an
    occurrence on or after min_date."""
    occurrences = {}
    for r in _read(directory, "CADORS_Occurrence_Information.csv"):
        day, at = _when(r.get("occurrencedate"), r.get("occurrencetime"))
        if day is None or day < min_date:
            continue
        occurrences[r["cadorsnumber"]] = (at, {
            "report": r["cadorsnumber"],
            "type": r.get("occurrencetypedescriptione") or None,
            "aerodrome": r.get("aerodromeid") or None,
            "location": r.get("occurrencelocation") or None,
            "country": r.get("country_enm") or None,
            "fatalities": _num(r.get("fatalities")),
            "injuries": _num(r.get("Injuries")),
            "tsb": r.get("tsboccurrencenumber") or None,
            "events": [],
        })
    for r in _read(directory, "CADORS_Occurrence_Event_Information.csv"):
        occ = occurrences.get(r.get("cadorsnumber"))
        name = r.get("event_name_enm")
        if occ and name and name not in occ[1]["events"]:
            occ[1]["events"].append(name)
    for r in _read(directory, "CADORS_Aircraft_Information.csv"):
        occ = occurrences.get(r.get("cadorsnumber"))
        reg = _registration(r)
        if not occ or not reg:
            continue
        at, base = occ
        yield reg, at, dict(base,
                            flight=r.get("flightnumber") or None,
                            phase=r.get("phasenamee") or None,
                            damage=r.get("damagedescriptione") or None)


def ingest_cadors(session, directory):
    """Attach CADORS occurrences to the airframes whose registration
    spells cover their dates. Returns the number of new events."""
    spells = {}
    for airframe_id, value, first, last in session.execute(
            select(AirframeSpell.airframe_id, AirframeSpell.value,
                   AirframeSpell.first_date, AirframeSpell.last_date)
            .where(AirframeSpell.kind == "registration")):
        spells.setdefault(_norm(value), []).append((airframe_id, first,
                                                    last))
    matched = {}
    for reg, at, detail in parse(directory):
        day = at.date()
        for airframe_id, first, last in spells.get(_norm(reg), ()):
            lo = (first - SLACK) if first else datetime.date.min
            hi = (last + SLACK) if last else datetime.date.max
            if lo <= day <= hi:
                matched[(airframe_id, at)] = dict(detail, registration=reg)
    if not matched:
        return 0
    ids = list({k[0] for k in matched})
    have = set()
    for start in range(0, len(ids), CHUNK):
        for aid, at in session.execute(
                select(AirframeEvent.airframe_id, AirframeEvent.at).where(
                    AirframeEvent.source == SOURCE,
                    AirframeEvent.airframe_id.in_(ids[start:start + CHUNK]))):
            have.add((aid, at.replace(tzinfo=None)))
    fresh = [{"airframe_id": aid, "kind": "occurrence", "at": at,
              "detail": detail, "source": SOURCE,
              "evidence_ref": "CADORS %s" % detail["report"],
              "visibility": "public"}
             for (aid, at), detail in matched.items()
             if (aid, at.replace(tzinfo=None)) not in have]
    for start in range(0, len(fresh), CHUNK):
        session.execute(AirframeEvent.__table__.insert(),
                        fresh[start:start + CHUNK])
    return len(fresh)
