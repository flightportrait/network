"""The FAA Releasable Aircraft database as a registry source.

US civil registry, public domain (a US government work), republished
daily at registry.faa.gov/database/ReleasableAircraft.zip. The import
reads the zip as downloaded: MASTER.txt (one row per registered
aircraft), ACFTREF.txt (manufacturer and model per model code) and
ENGINE.txt (engine per engine code).

Two writes (docs/system/AIRFRAMES.md §5):

- ref_airframes, at rank "registry": registration and year for every
  US aircraft. Owner names do not go here: the registry names owners
  and lessors, not who flies the aircraft.
- the lifetime record, for airframes the network has observed: claims
  (serial number, build year, manufacturer, model, engine, certificate
  and airworthiness dates, registry status, company owner), the MSN and
  build year on the airframe row, and dated "registered" and
  "airworthiness" events. Registry-only aircraft get a record the day
  the network first hears them.

Personal data stays out (AIRFRAMES.md D7): owner names only for
corporations, LLCs, government and non-citizen corporations; never an
individual, a partnership or co-owners, and never an address.
"""
import csv
import datetime
import io
import zipfile

from sqlalchemy import select, update

from .refdata_models import Airframe, AirframeClaim, AirframeEvent, \
    AirframeSpell

SOURCE = "faa"
LICENCE = "public-domain"
REF = "registry.faa.gov ReleasableAircraft"
# FAA TYPE REGISTRANT codes whose names are companies or governments:
# 3 corporation, 5 government, 7 LLC, 8 non-citizen corporation.
COMPANY_REGISTRANTS = {"3", "5", "7", "8"}
CHUNK = 5000


def _rows(zf, name):
    with zf.open(name) as raw:
        reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig",
                                             errors="replace"))
        header = [h.strip() for h in next(reader)]
        for row in reader:
            yield {header[i]: row[i].strip()
                   for i in range(min(len(header), len(row)))}


def _date(value):
    try:
        return datetime.datetime.strptime(value, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _year(value):
    return int(value) if value.isdigit() and 1900 < int(value) < 2100 \
        else None


def parse(path):
    """Yield one dict per registered aircraft with a Mode S address."""
    with zipfile.ZipFile(path) as zf:
        models = {r["CODE"]: (r.get("MFR", ""), r.get("MODEL", ""))
                  for r in _rows(zf, "ACFTREF.txt")}
        engines = {r["CODE"]: " ".join(
            x for x in (r.get("MFR", ""), r.get("MODEL", "")) if x)
            for r in _rows(zf, "ENGINE.txt")}
        for r in _rows(zf, "MASTER.txt"):
            hex_id = r.get("MODE S CODE HEX", "").lower()
            if len(hex_id) != 6 or not r.get("N-NUMBER"):
                continue
            mfr, model = models.get(r.get("MFR MDL CODE", ""), ("", ""))
            owner = r.get("NAME") if r.get("TYPE REGISTRANT") in \
                COMPANY_REGISTRANTS else None
            yield {
                "hex": hex_id,
                "registration": "N" + r["N-NUMBER"],
                "msn": r.get("SERIAL NUMBER") or None,
                "year": _year(r.get("YEAR MFR", "")),
                "manufacturer": mfr or None,
                "model": model or None,
                "engine": engines.get(r.get("ENG MFR MDL", "")) or None,
                "certificate_date": _date(r.get("CERT ISSUE DATE")),
                "airworthiness_date": _date(r.get("AIR WORTH DATE")),
                "status": r.get("STATUS CODE") or None,
                "owner": owner or None,
            }


def _claims(airframe_id, rec):
    fields = {
        "msn": rec["msn"], "built_year": rec["year"],
        "manufacturer": rec["manufacturer"], "model": rec["model"],
        "engine": rec["engine"], "registration": rec["registration"],
        "certificate_date": rec["certificate_date"],
        "airworthiness_date": rec["airworthiness_date"],
        "registry_status": rec["status"], "owner": rec["owner"],
    }
    for field, value in fields.items():
        if value is None:
            continue
        value = value.isoformat() if isinstance(value, datetime.date) \
            else str(value)
        yield field, value[:200]


def _upsert_claims(session, rows, now):
    """rows: [(airframe_id, field, value)]. Same statement again moves
    last_seen; a new value is a new row."""
    ids = {r[0] for r in rows}
    existing = {}
    id_list = list(ids)
    for start in range(0, len(id_list), CHUNK):
        for c in session.execute(select(AirframeClaim).where(
                AirframeClaim.source == SOURCE,
                AirframeClaim.airframe_id.in_(id_list[start:start + CHUNK])
        )).scalars():
            existing[(c.airframe_id, c.field, c.value)] = c.id
    seen, fresh = [], []
    for key in rows:
        if key in existing:
            seen.append(existing[key])
        else:
            fresh.append({"airframe_id": key[0], "field": key[1],
                          "value": key[2], "source": SOURCE,
                          "source_ref": REF, "licence": LICENCE,
                          "first_seen": now, "last_seen": now})
    for start in range(0, len(seen), CHUNK):
        session.execute(update(AirframeClaim)
                        .where(AirframeClaim.id.in_(seen[start:start + CHUNK]))
                        .values(last_seen=now))
    for start in range(0, len(fresh), CHUNK):
        session.execute(AirframeClaim.__table__.insert(),
                        fresh[start:start + CHUNK])
    return len(fresh)


def ingest_faa(session, path):
    """Import the FAA zip. Returns the number of registry rows read."""
    from .refdata_ingest import _merge_streaming

    records = list(parse(path))

    def pairs():
        for rec in records:
            yield rec["hex"], {"registration": rec["registration"][:16],
                               "year": rec["year"]}
    _merge_streaming(session, pairs(), "registry")

    by_hex = dict(session.execute(
        select(AirframeSpell.value, AirframeSpell.airframe_id)
        .where(AirframeSpell.kind == "hex")
        .order_by(AirframeSpell.first_date)).all())
    wanted = list({by_hex[r["hex"]] for r in records if r["hex"] in by_hex})
    frames = {}
    for start in range(0, len(wanted), CHUNK):
        frames.update((f.id, f) for f in session.execute(
            select(Airframe).where(
                Airframe.id.in_(wanted[start:start + CHUNK]))).scalars())
    now = datetime.datetime.now(datetime.timezone.utc)
    claims, events = [], []
    for rec in records:
        airframe_id = by_hex.get(rec["hex"])
        if airframe_id is None:
            continue
        frame = frames[airframe_id]
        claims.extend((airframe_id, f, v) for f, v in _claims(airframe_id,
                                                               rec))
        if rec["year"] and frame.built_year is None:
            frame.built_year = rec["year"]
        if rec["manufacturer"] and frame.manufacturer is None:
            frame.manufacturer = rec["manufacturer"][:80]
        if rec["msn"] and frame.msn is None:
            frame.msn = rec["msn"][:32]
        for kind, day in (("registered", rec["certificate_date"]),
                          ("airworthiness", rec["airworthiness_date"])):
            if day:
                events.append((airframe_id, kind, day, rec["registration"]))
    session.flush()
    _upsert_claims(session, claims, now)

    have = set()
    ids = list({e[0] for e in events})
    for start in range(0, len(ids), CHUNK):
        for aid, kind, at in session.execute(
                select(AirframeEvent.airframe_id, AirframeEvent.kind,
                       AirframeEvent.at)
                .where(AirframeEvent.source == SOURCE,
                       AirframeEvent.airframe_id.in_(ids[start:start + CHUNK]))):
            have.add((aid, kind, at.date()))
    fresh = [{"airframe_id": aid, "kind": kind,
              "at": datetime.datetime.combine(day, datetime.time(),
                                              tzinfo=datetime.timezone.utc),
              "detail": {"registration": reg}, "source": SOURCE,
              "evidence_ref": REF, "visibility": "public"}
             for aid, kind, day, reg in events
             if (aid, kind, day) not in have]
    for start in range(0, len(fresh), CHUNK):
        session.execute(AirframeEvent.__table__.insert(),
                        fresh[start:start + CHUNK])
    candidates = same_aircraft_candidates(session)
    if candidates:
        print("faa: %d serial numbers name more than one airframe with the "
              "same manufacturer and model (an earlier hex); left for "
              "review" % len(candidates))
    return len(records)


def same_aircraft_candidates(session):
    """(manufacturer, model, msn) the registry has stated for more than
    one airframe: the same aircraft under an earlier hex, the D2 merge
    case. Reported for review, never merged here."""
    rows = session.execute(select(AirframeClaim.airframe_id,
                                  AirframeClaim.field, AirframeClaim.value)
                           .where(AirframeClaim.source == SOURCE,
                                  AirframeClaim.field.in_(
                                      ("manufacturer", "model", "msn"))))
    per = {}
    for airframe_id, field, value in rows:
        per.setdefault(airframe_id, {}).setdefault(field, set()).add(value)
    seen = {}
    for airframe_id, f in per.items():
        for mfr in f.get("manufacturer", ()):
            for model in f.get("model", ()):
                for msn in f.get("msn", ()):
                    seen.setdefault((mfr, model, msn), set()).add(airframe_id)
    return [k for k, ids in seen.items() if len(ids) > 1]
