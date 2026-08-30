"""Reference-data importers.

Idempotent file-eaters, one subcommand per source — re-running any of
them is always safe, and every run leaves a ref_imports row. All input
is local files: artifacts produced by the derivation export jobs, or
snapshots fetched by whoever runs the imports. The
importer itself never talks to the network — same testability rule as
everything else in this service.

    python -m app.refdata_ingest airports  airports.csv
    python -m app.refdata_ingest tar1090   aircraft.csv.gz
    python -m app.refdata_ingest airframes airframes.json.gz   # derived artifact
    python -m app.refdata_ingest seed      refdata_seed.json   # derived artifact
    python -m app.refdata_ingest routes    routes.json.gz      # derived artifact
    python -m app.refdata_ingest airport_tz openflights.dat    # OpenFlights tz
    python -m app.refdata_ingest leg_stats [legs.db]           # derived artifact
    python -m app.refdata_ingest schedule  [legs.db]           # needs callsign
    python -m app.refdata_ingest alliances refdata/alliances.json
    python -m app.refdata_ingest derive                        # after routes/seed

Merge policy for ref_airframes is SOURCE_RANK: equal or
higher rank overwrites non-null fields, lower rank only fills nulls.
"""
import argparse
import csv
import datetime
import gzip
import json
import re
import sqlite3
import sys

from sqlalchemy import delete, func, insert, select, update

from .db import make_sessionmaker
from .refdata_models import (RefAirframe, RefAirline, RefAirlineCountry,
                             RefAlliance, RefAllianceMembership, RefAirport,
                             RefImport, RefLegStat, RefRoute, RefSchedule,
                             RefType, SOURCE_RANK)
from .settings import Settings


CHUNK = 5000

# Airframe fields the precedence merge applies to.
_MERGE_FIELDS = ("registration", "type_code", "operator_name",
                 "operator_norm", "operator_icao", "year", "flags")


def _clean(value, limit):
    value = (value or "").strip()
    return value[:limit] if value else None


def _open_text(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


def _merge_airframes(session, incoming, source):
    """Upsert batches of {hex: fields} under the precedence rule. Callers
    stream CHUNK-sized batches — the full registry never sits in memory
    at once, so the service stays within a small memory budget."""
    rank = SOURCE_RANK[source]
    written = 0
    hexes = list(incoming)
    for start in range(0, len(hexes), CHUNK):
        chunk = hexes[start:start + CHUNK]
        existing = {
            row.hex: row for row in session.execute(
                select(RefAirframe).where(RefAirframe.hex.in_(chunk))
            ).scalars()
        }
        for hex_id in chunk:
            fields = incoming[hex_id]
            row = existing.get(hex_id)
            if row is None:
                session.add(RefAirframe(hex=hex_id, source=source, **fields))
                written += 1
                continue
            outranked = rank < SOURCE_RANK.get(row.source, 0)
            changed = False
            for name in _MERGE_FIELDS:
                value = fields.get(name)
                if value is None:
                    continue
                if outranked and getattr(row, name) is not None:
                    continue          # lower rank only fills silence
                if getattr(row, name) != value:
                    setattr(row, name, value)
                    changed = True
            if changed:
                if not outranked:
                    row.source = source
                written += 1
        session.flush()
        session.expunge_all()
    return written


def _merge_streaming(session, pairs, source):
    """Drain an iterator of (hex, fields) through _merge_airframes in
    CHUNK-sized batches."""
    written = 0
    batch = {}
    for hex_id, fields in pairs:
        batch[hex_id] = fields
        if len(batch) >= CHUNK:
            written += _merge_airframes(session, batch, source)
            batch = {}
    if batch:
        written += _merge_airframes(session, batch, source)
    return written


def ingest_tar1090(session, path):
    """wiedehopf/tar1090-db csv branch: hex;reg;type;dbflags;desc;year;ownOp;
    (format verified against the live file, 2026-08-28)."""
    def pairs():
        with _open_text(path) as fh:
            for line in fh:
                parts = line.rstrip("\n").split(";")
                if len(parts) < 7 or len(parts[0]) != 6:
                    continue
                operator = _clean(parts[6], 120)
                year = parts[5].strip()
                yield parts[0].strip().lower(), {
                    "registration": _clean(parts[1], 16),
                    "type_code": _clean(parts[2], 8),
                    "operator_name": operator,
                    "operator_norm": operator.upper() if operator else None,
                    "year": int(year) if year.isdigit() else None,
                    "flags": _clean(parts[3], 8),
                }
    return _merge_streaming(session, pairs(), "tar1090")


def ingest_airframes(session, path):
    """The airframes artifact: gzipped {hex: [type, registration,
    operator_icao]}. type/reg come from ODbL dump headers; operator_icao
    is the airframe's majority observed callsign prefix — the claim
    registries can't make."""
    with _open_text(path) as fh:
        data = json.load(fh)

    def pairs():
        for hex_id, values in data.items():
            if len(hex_id) != 6:
                continue
            type_code, registration, operator = (list(values)
                                                 + [None] * 3)[:3]
            yield hex_id.strip().lower(), {
                "registration": _clean(registration, 16),
                "type_code": _clean(type_code, 8),
                "operator_name": None, "operator_norm": None,
                "operator_icao": _clean(operator, 3),
                "year": None, "flags": None,
            }
    return _merge_streaming(session, pairs(), "fp-dump")


def ingest_airports(session, path):
    """OurAirports airports.csv (public domain). Keyed by ident, which is
    ICAO-style where one exists — the same idents route chains use."""
    written = 0
    with _open_text(path) as fh:
        for row in csv.DictReader(fh):
            ident = _clean(row.get("ident"), 8)
            if not ident:
                continue
            values = {
                "name": _clean(row.get("name"), 120),
                "kind": _clean(row.get("type"), 20),
                "iso_country": _clean(row.get("iso_country"), 2),
                "municipality": _clean(row.get("municipality"), 80),
                "iata": _clean(row.get("iata_code"), 3),
            }
            try:
                values["lat"] = float(row.get("latitude_deg") or "")
                values["lon"] = float(row.get("longitude_deg") or "")
            except ValueError:
                values["lat"] = values["lon"] = None
            session.merge(RefAirport(ident=ident.upper(), **values))
            written += 1
            if written % CHUNK == 0:
                session.flush()
                session.expunge_all()
    return written


def ingest_airport_tz(session, path):
    """OpenFlights airports.dat: attach an Olson timezone to each airport
    we already have, so departure times can be shown in the airport's own
    local time. CSV columns: ID,Name,City,Country,IATA,ICAO,Lat,Lon,Alt,
    TZoffset,DST,Tz,Type,Source — we take ICAO->ident (primary) and
    IATA->iata (fallback) as the join key."""
    by_ident, by_iata = {}, {}
    with _open_text(path) as fh:
        for row in csv.reader(fh):
            if len(row) < 12:
                continue
            icao, iata, tz = row[5].strip(), row[4].strip(), row[11].strip()
            if "/" not in tz:                 # only real Olson names
                continue
            if len(icao) == 4 and icao != "\\N":
                by_ident[icao.upper()] = tz
            if len(iata) == 3 and iata != "\\N":
                by_iata.setdefault(iata.upper(), tz)

    written = 0
    if by_ident:
        # Only idents we actually hold — the ORM bulk update raises if a
        # primary key in the batch matches no row (OpenFlights lists a few
        # hundred airports OurAirports doesn't).
        existing = set(session.execute(select(RefAirport.ident)).scalars())
        pairs = [{"ident": k, "tz": v}
                 for k, v in by_ident.items() if k in existing]
        if pairs:
            session.execute(update(RefAirport), pairs)
    # IATA fallback for airports OpenFlights lists without an ICAO code.
    for iata, tz in by_iata.items():
        r = session.execute(
            update(RefAirport).where(RefAirport.iata == iata,
                                     RefAirport.tz.is_(None)).values(tz=tz))
        written += r.rowcount or 0
    total = session.execute(
        select(func.count()).select_from(RefAirport)
        .where(RefAirport.tz.is_not(None))).scalar_one()
    print("  airports with tz now: %d" % total)
    return total


def ingest_boards(session, path):
    """Merge harvested airport boards (boards.db, the private
    collector's artifact) into the schedule:

    - A published departure that matches an observed service on the
      same leg within 45 minutes DECORATES that row: the marketed
      flight number lands in `flight`, source becomes `both`. The
      observed times stay — observation is the reality check.
    - A published departure with no observed counterpart becomes a new
      row, source `published` — this is where boards extend coverage
      beyond what receivers hear.
    - Published arrivals fill arr_min where observation could not.

    The board's modal time per (flight, leg) over its harvested days is
    used, same clustering philosophy as the observed derive."""
    import os
    if not os.path.exists(path):
        print("  boards: no artifact at %s — skipped" % path)
        return 0
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    deps, arrs = {}, {}
    try:
        for airport, kind, flight, cp, sched, c in conn.execute(
                "SELECT airport, kind, flight, counterpart, sched_min,"
                " COUNT(*) FROM boards WHERE counterpart IS NOT NULL"
                " AND sched_min IS NOT NULL"
                " GROUP BY airport, kind, flight, counterpart, sched_min"
                " ORDER BY airport, kind, flight, counterpart,"
                " COUNT(*) DESC"):
            if kind == "dep":
                deps.setdefault((flight, airport, cp), (sched, c))
            else:
                arrs.setdefault((flight, cp, airport), sched)
        days = dict(conn.execute(
            "SELECT flight || '|' || airport || '|' || counterpart,"
            " COUNT(DISTINCT day) FROM boards WHERE kind = 'dep'"
            " GROUP BY 1").fetchall())
    finally:
        conn.close()

    iata_icao = {a.iata: a.icao for a in session.execute(
        select(RefAirline).where(RefAirline.iata.is_not(None))).scalars()}

    decorated = added = arrfill = 0
    for (flight, org, dst), (sched, _n) in deps.items():
        rows = session.execute(
            select(RefSchedule).where(RefSchedule.org == org,
                                      RefSchedule.dst == dst,
                                      RefSchedule.dep_min.is_not(None))
        ).scalars().all()
        best = None
        for r in rows:
            d = abs(r.dep_min - sched)
            if d <= 45 and (best is None or d < best[0]):
                best = (d, r)
        if best is not None:
            r = best[1]
            r.flight = flight[:8]
            r.source = "both"
            decorated += 1
            continue
        prefix = "".join(ch for ch in flight if ch.isalpha())[:2]
        icao = iata_icao.get(prefix, "")
        session.merge(RefSchedule(
            callsign=flight[:12], org=org, dst=dst,
            airline_icao=icao if icao else
            (prefix if prefix.isalpha() else ""),
            dep_min=sched, arr_min=arrs.get((flight, org, dst)),
            type_code=None, flight=flight[:8], source="published",
            n_flights=days.get("|".join((flight, org, dst)), 1)))
        added += 1
    for (flight, org, dst), arr in arrs.items():
        row = session.execute(
            select(RefSchedule).where(RefSchedule.flight == flight[:8],
                                      RefSchedule.org == org,
                                      RefSchedule.dst == dst)
        ).scalars().first()
        if row is not None and row.arr_min is None:
            row.arr_min = arr
            arrfill += 1
    print("  boards: %d services decorated with flight numbers,"
          " %d published-only added, %d arrivals filled"
          % (decorated, added, arrfill))
    return decorated + added


def ingest_airline_names(session, path):
    """Complete the airline registry from observation: every operator
    the schedule table actually records gets a row, named from the
    OpenFlights airlines.dat snapshot (ODbL, same source family as the
    airport timezones). The curated seed rows keep their names and
    palettes; operators OpenFlights cannot name are left out rather
    than shown as bare codes."""
    names = {}
    with _open_text(path) as fh:
        for row in csv.reader(fh):
            if len(row) < 8:
                continue
            name, iata, icao, active = (row[1].strip(), row[3].strip(),
                                        row[4].strip(), row[7].strip())
            if (len(icao) != 3 or not icao.isalpha() or not name
                    or name == "Unknown"):
                continue
            if icao.upper() in names and active != "Y":
                continue                  # prefer the active holder
            names[icao.upper()] = (name[:120],
                                   iata if len(iata) == 2 else None)

    have = set(session.execute(select(RefAirline.icao)).scalars())
    observed = set(session.execute(
        select(RefSchedule.airline_icao).distinct()
        .where(RefSchedule.airline_icao != "",
               RefSchedule.n_flights >= 5)).scalars())
    added = 0
    for icao in sorted(observed - have):
        found = names.get(icao)
        if not found:
            continue
        session.add(RefAirline(icao=icao, iata=found[1], name=found[0],
                               palette=[]))
        added += 1
    return added


_ALLIANCE_RELATIONSHIPS = {"member", "group-brand", "affiliate"}
_ALLIANCE_STATUSES = {"active", "future", "suspended", "former"}


def _iso_date(value, field, required=False):
    if not value:
        if required:
            raise ValueError("%s is required" % field)
        return None
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be YYYY-MM-DD" % field) from exc


def ingest_alliances(session, path):
    """Replace the global-alliance roster from one curated official snapshot.

    The source file carries airline names because membership is useful even
    before our receivers observe a member. Missing official members are added
    to ref_airlines; existing curated airline names and palettes always win.
    Membership is never inferred from ownership, codeshare, or callsign.
    """
    with _open_text(path) as fh:
        data = json.load(fh)
    if data.get("schema_version") != 1:
        raise ValueError("unsupported alliance schema_version")
    as_of = _iso_date(data.get("as_of"), "as_of", required=True)
    specs = data.get("alliances")
    if not isinstance(specs, list) or not specs:
        raise ValueError("alliances must be a non-empty list")

    alliances, memberships = [], []
    seen_slugs, seen_rows, active_direct = set(), set(), {}
    for spec in specs:
        slug = (spec.get("slug") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ValueError("invalid alliance slug: %r" % slug)
        if slug in seen_slugs:
            raise ValueError("duplicate alliance slug: %s" % slug)
        seen_slugs.add(slug)
        name = _clean(spec.get("name"), 48)
        website = _clean(spec.get("website_url"), 255)
        source = _clean(spec.get("source_url"), 255)
        if not name or not website or not source:
            raise ValueError("%s needs name, website_url and source_url" % slug)
        checked = _iso_date(spec.get("source_checked_at") or data.get("as_of"),
                            "%s.source_checked_at" % slug, required=True)
        if checked > as_of:
            raise ValueError("%s was checked after snapshot as_of" % slug)
        alliances.append({
            "slug": slug, "name": name, "website_url": website,
            "source_url": source, "source_checked_at": checked,
            "logo_asset_url": _clean(spec.get("logo_asset_url"), 255),
        })
        rows = spec.get("memberships")
        if not isinstance(rows, list) or not rows:
            raise ValueError("%s.memberships must be a non-empty list" % slug)
        for raw in rows:
            code = (raw.get("airline_icao") or "").strip().upper()
            airline_name = _clean(raw.get("airline_name"), 120)
            iata = (raw.get("airline_iata") or "").strip().upper() or None
            relationship = (raw.get("relationship") or "member").strip()
            status = (raw.get("status") or "active").strip()
            sponsor = (raw.get("sponsor_icao") or "").strip().upper() or None
            if not re.fullmatch(r"[A-Z]{3}", code) or not airline_name:
                raise ValueError("%s membership needs ICAO and airline_name" % slug)
            if iata and not re.fullmatch(r"[A-Z0-9]{2}", iata):
                raise ValueError("invalid IATA code for %s" % code)
            if relationship not in _ALLIANCE_RELATIONSHIPS:
                raise ValueError("invalid relationship for %s" % code)
            if status not in _ALLIANCE_STATUSES:
                raise ValueError("invalid status for %s" % code)
            if relationship == "member" and sponsor:
                raise ValueError("direct member %s cannot have a sponsor" % code)
            if relationship != "member" and not sponsor:
                raise ValueError("%s relationship for %s needs sponsor_icao"
                                 % (relationship, code))
            effective_from = _iso_date(raw.get("effective_from"),
                                       "%s.effective_from" % code)
            effective_to = _iso_date(raw.get("effective_to"),
                                     "%s.effective_to" % code)
            if effective_from and effective_to and effective_to < effective_from:
                raise ValueError("effective interval is backwards for %s" % code)
            key = (slug, code, relationship, status, effective_from, effective_to)
            if key in seen_rows:
                raise ValueError("duplicate membership row: %s/%s" % (slug, code))
            seen_rows.add(key)
            if status == "active" and relationship == "member":
                other = active_direct.get(code)
                if other and other != slug:
                    raise ValueError("%s is active in both %s and %s"
                                     % (code, other, slug))
                active_direct[code] = slug
            memberships.append({
                "alliance_slug": slug, "airline_icao": code,
                "airline_name": airline_name, "airline_iata": iata,
                "relationship": relationship, "status": status,
                "sponsor_icao": sponsor, "effective_from": effective_from,
                "effective_to": effective_to,
                "source_url": _clean(raw.get("source_url"), 255) or source,
                "source_checked_at": _iso_date(
                    raw.get("source_checked_at") or checked.isoformat(),
                    "%s.source_checked_at" % code, required=True),
                "note": _clean(raw.get("note"), 240),
            })

    # Affiliates and group brands must point at a current direct member in
    # the same alliance. This is intentionally stricter than accepting an
    # ownership claim or a codeshare list.
    direct = {(m["alliance_slug"], m["airline_icao"])
              for m in memberships
              if m["status"] == "active" and m["relationship"] == "member"}
    for m in memberships:
        if (m["relationship"] != "member"
                and (m["alliance_slug"], m["sponsor_icao"]) not in direct):
            raise ValueError("%s sponsor %s is not an active direct member"
                             % (m["airline_icao"], m["sponsor_icao"]))

    # Make the official denominator complete without overwriting better
    # airline reference data already present.
    for m in memberships:
        airline = session.get(RefAirline, m["airline_icao"])
        if airline is None:
            session.add(RefAirline(icao=m["airline_icao"],
                                   iata=m["airline_iata"],
                                   name=m["airline_name"], palette=[]))
        elif airline.iata is None and m["airline_iata"]:
            airline.iata = m["airline_iata"]
    session.flush()

    session.execute(delete(RefAllianceMembership))
    session.execute(delete(RefAlliance))
    session.flush()
    session.execute(insert(RefAlliance), alliances)
    for m in memberships:
        values = {k: v for k, v in m.items()
                  if k not in ("airline_name", "airline_iata")}
        session.add(RefAllianceMembership(**values))
    return len(alliances) + len(memberships)


def ingest_seed(session, path):
    """The seed artifact:
    {"airlines": {icao: {name, iata, palette}}, "types": {designator:
    {name, category}}} — compiled public facts, no artwork."""
    with _open_text(path) as fh:
        data = json.load(fh)
    written = 0
    for icao, airline in data.get("airlines", {}).items():
        session.merge(RefAirline(
            icao=icao.upper()[:3],
            iata=_clean(airline.get("iata"), 2),
            name=airline["name"][:120],
            palette=airline.get("palette") or None,
        ))
        written += 1
    for designator, spec in data.get("types", {}).items():
        session.merge(RefType(
            designator=designator.upper()[:4],
            name=spec["name"][:80],
            category=_clean(spec.get("category"), 12),
        ))
        written += 1
    return written


def ingest_routes(session, path):
    """The same routes.json.gz the RouteBook serves live — here it lands
    in a table so the derived views can join it."""
    with _open_text(path) as fh:
        data = json.load(fh)
    written = 0
    for callsign, chain in data.items():
        callsign = callsign.strip().upper()[:12]
        if not callsign or not isinstance(chain, list) or len(chain) < 2:
            continue
        session.merge(RefRoute(callsign=callsign, chain=chain,
                               airline_icao=None))
        written += 1
        if written % CHUNK == 0:
            session.flush()
            session.expunge_all()
    return written


def derive(session):
    """Recompute everything derived: route→airline resolution, then the
    airline→countries rollup. Cheap enough to run after every ingest."""
    airlines = set(session.execute(select(RefAirline.icao)).scalars())
    # Route chains use IATA codes (that is what the route derivation
    # emits — KUL, SIN); resolve those first, ICAO idents as fallback.
    by_ident = dict(session.execute(
        select(RefAirport.ident, RefAirport.iso_country)
        .where(RefAirport.iso_country.is_not(None))).all())
    by_iata = dict(session.execute(
        select(RefAirport.iata, RefAirport.iso_country)
        .where(RefAirport.iata.is_not(None),
               RefAirport.iso_country.is_not(None))).all())

    counts = {}
    n_routes = 0
    for route in session.execute(select(RefRoute)).scalars():
        prefix = route.callsign[:3]
        icao = prefix if (len(route.callsign) > 3 and prefix.isalpha()
                          and prefix in airlines) else None
        if route.airline_icao != icao:
            route.airline_icao = icao
        if icao is None:
            continue
        n_routes += 1
        touched = {by_iata.get(code) or by_ident.get(code)
                   for code in route.chain}
        for country in touched - {None}:
            counts[(icao, country)] = counts.get((icao, country), 0) + 1

    session.execute(delete(RefAirlineCountry))
    session.flush()
    for (icao, country), n in counts.items():
        session.add(RefAirlineCountry(airline_icao=icao,
                                      iso_country=country, n_routes=n))
    return n_routes


# Real airport codes are IATA (3) or ICAO (4); the artifact carries a few
# hundred longer placeholder codes that resolve to no airport. Filtering
# by length here drops that junk AND keeps codes within the String(4)
# column, so no truncation (which would collide distinct codes) is needed.
_LEG_WHERE = ("l.org IS NOT NULL AND l.dst IS NOT NULL AND l.org <> l.dst"
              " AND length(l.org) BETWEEN 3 AND 4"
              " AND length(l.dst) BETWEEN 3 AND 4"
              " AND (l.dep_ts IS NULL OR l.arr_ts IS NULL"
              "      OR l.arr_ts - l.dep_ts >= 600)")


def ingest_leg_stats(session, path):
    """Aggregate the legs.db flight-log into per-airline route-leg
    statistics (frequency + which aircraft flew each leg).

    The heavy grouping over millions of legs is pushed into SQLite, not
    done with a Python accumulator: a per-leg date set per airport-pair
    would blow the service's small memory budget, while SQLite's
    count(DISTINCT date) spills to a temp file. We build a scratch table
    of hex -> observed operator (the identity we already resolved onto
    each airframe, so this inherits the route-evidence gate),
    ATTACH the artifact read-only, and let one join+GROUP BY do the work.

    Run it as a separate one-off process, not inside the live service,
    so it does not share memory with uvicorn."""
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error:
        raise SystemExit(
            "legs.db has no readable meta table — likely mid-delivery; "
            "retry once the artifact export completes")
    finally:
        conn.close()
    window_days = int(meta.get("window_days") or 60)
    generated_at = meta.get("generated_at") or ""
    # The frequency denominator is the artifact's OWN date span — never
    # a constant that goes stale when the archive deepens, and immune to
    # dedupe/stitch reshuffles because MIN/MAX over dates ignore both.
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM legs")\
            .fetchone()
    finally:
        conn.close()
    try:
        span = (datetime.date.fromisoformat(hi)
                - datetime.date.fromisoformat(lo)).days + 1
    except (TypeError, ValueError):
        span = window_days
    observed_days = max(1, min(window_days, span))

    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    work = sqlite3.connect("file:%s" % tmp, uri=True)
    try:
        # Sort/group on disk with a small page cache — the three GROUP BYs
        # over millions of joined legs must stay within the service's
        # small memory budget.
        work.execute("PRAGMA temp_store=FILE")
        work.execute("PRAGMA cache_size=-16000")   # ~16 MB
        work.execute("CREATE TABLE op (hex TEXT PRIMARY KEY, icao TEXT)")
        result = session.execute(
            select(RefAirframe.hex, RefAirframe.operator_icao)
            .where(RefAirframe.operator_icao.is_not(None))
            .execution_options(stream_results=True, yield_per=20000))
        work.executemany("INSERT OR IGNORE INTO op VALUES (?, ?)",
                         ((h, i) for h, i in result))
        work.commit()
        work.execute("ATTACH DATABASE ? AS L", ("file:%s?mode=ro" % path,))

        totals = {}
        for icao, o, d, n, nd, avg_min in work.execute(
                "SELECT op.icao, min(l.org,l.dst) o, max(l.org,l.dst) d,"
                " count(*), count(DISTINCT l.date),"
                " CAST(avg(CASE WHEN l.dep_ts IS NOT NULL"
                "   AND l.arr_ts > l.dep_ts"
                "   AND l.arr_ts - l.dep_ts BETWEEN 600 AND 72000"
                "   THEN (l.arr_ts - l.dep_ts) / 60.0 END) AS INT)"
                " FROM L.legs l JOIN op ON op.hex = l.hex"
                " WHERE " + _LEG_WHERE + " GROUP BY op.icao, o, d"):
            totals[(icao, o, d)] = (n, nd, avg_min)

        types_by_key = {}
        for icao, o, d, typ, c in work.execute(
                "SELECT op.icao, min(l.org,l.dst) o, max(l.org,l.dst) d,"
                " l.type, count(*) c"
                " FROM L.legs l JOIN op ON op.hex = l.hex"
                " WHERE " + _LEG_WHERE + " AND l.type IS NOT NULL"
                " AND l.type <> '' GROUP BY op.icao, o, d, l.type"
                " ORDER BY op.icao, o, d, c DESC"):
            lst = types_by_key.setdefault((icao, o, d), [])
            if len(lst) < 4:
                lst.append([typ, c])

        # Phase 1: insert totals + types, then FREE those dicts before the
        # airframes pass. Holding all three at once (plus the insert batch)
        # over-runs the small memory budget the service is held to.
        session.execute(delete(RefLegStat))
        session.flush()
        rows, batch = 0, []
        for (icao, o, d), (n, nd, avg_min) in totals.items():
            batch.append({
                "airline_icao": icao, "o": o, "d": d,
                "n_flights": n, "n_days": nd, "avg_min": avg_min,
                "per_week": round(n / observed_days * 7, 2),
                "types": types_by_key.get((icao, o, d)) or [],
                "airframes": []})
            if len(batch) >= CHUNK:
                session.execute(insert(RefLegStat), batch)
                rows += len(batch)
                batch = []
        if batch:
            session.execute(insert(RefLegStat), batch)
            rows += len(batch)
        totals = types_by_key = None

        # Phase 2: the airframes pass, then bulk-UPDATE by primary key.
        # Store only [hex, count]; reg/type resolve from ref_airframes at
        # request time, keeping this dict small.
        frames_by_key = {}
        for icao, o, d, hexid, c in work.execute(
                "SELECT op.icao, min(l.org,l.dst) o, max(l.org,l.dst) d,"
                " l.hex, count(*) c"
                " FROM L.legs l JOIN op ON op.hex = l.hex"
                " WHERE " + _LEG_WHERE + " GROUP BY op.icao, o, d, l.hex"
                " ORDER BY op.icao, o, d, c DESC"):
            lst = frames_by_key.setdefault((icao, o, d), [])
            if len(lst) < 6:
                lst.append([hexid, c])
    finally:
        work.close()
        os.unlink(tmp)

    upd = []
    for (icao, o, d), frames in frames_by_key.items():
        upd.append({"airline_icao": icao, "o": o, "d": d, "airframes": frames})
        if len(upd) >= CHUNK:
            session.execute(update(RefLegStat), upd)
            upd = []
    if upd:
        session.execute(update(RefLegStat), upd)
    print("  legs.db window=%dd observed=%dd generated=%s"
          % (window_days, observed_days, generated_at[:10]))
    return rows


def _local_min(ts, tzname, cache):
    """UTC epoch -> minute-of-day in tzname, DST-correct for that date.
    None when the timezone is unknown; results memoised per zone."""
    from datetime import datetime
    zi = cache.get(tzname)
    if zi is None:
        from zoneinfo import ZoneInfo
        try:
            zi = ZoneInfo(tzname)
        except Exception:                       # noqa: BLE001
            zi = False
        cache[tzname] = zi
    if not zi or ts is None:
        return None
    dt = datetime.fromtimestamp(ts, zi)
    return dt.hour * 60 + dt.minute


SCHEDULE_WINDOW_DAYS = 60


def ingest_schedule(session, path):
    """Infer the CURRENT timetable from legs.db: per flight number and
    leg, the typical local departure and arrival. Two rules keep it
    honest against a deep archive:

    - Recency: only the last SCHEDULE_WINDOW_DAYS of legs feed it. A
      year of observation is depth for logs and stats; a timetable
      claims "now", and a year of seasonal renumbering read as noise.
    - Paired arrivals: the arrival is the MEDIAN arrival of the legs
      that departed in the flight's modal slot. Moding departures and
      arrivals independently once mixed populations into physically
      impossible pairs.

    Times are converted to each airport's LOCAL clock before
    clustering, so daylight saving folds into one slot. The work is
    offloaded to a SQLite table; memory stays flat."""
    tz_by_code = {}
    for ident, iata, tz in session.execute(
            select(RefAirport.ident, RefAirport.iata, RefAirport.tz)
            .where(RefAirport.tz.is_not(None))):
        if ident:
            tz_by_code[ident] = tz
        if iata:
            tz_by_code.setdefault(iata, tz)

    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    src = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    hi = src.execute("SELECT MAX(date) FROM legs").fetchone()[0]
    try:
        cutoff = (datetime.date.fromisoformat(hi)
                  - datetime.timedelta(days=SCHEDULE_WINDOW_DAYS)
                  ).isoformat()
    except (TypeError, ValueError):
        cutoff = "0000"
    work = sqlite3.connect("file:%s" % tmp, uri=True)
    try:
        work.execute("PRAGMA temp_store=FILE")
        work.execute("PRAGMA cache_size=-16000")
        work.execute("CREATE TABLE s (callsign TEXT, org TEXT, dst TEXT,"
                     " dep5 INT, arr5 INT, type TEXT)")
        zones, batch = {}, []

        def rows():
            for cs, org, dst, dep_ts, dep_arr, typ in src.execute(
                    "SELECT callsign, org, dst, dep_ts, arr_ts, type"
                    " FROM legs WHERE callsign IS NOT NULL AND callsign <> ''"
                    " AND org IS NOT NULL AND dst IS NOT NULL AND org <> dst"
                    " AND length(org) BETWEEN 3 AND 4"
                    " AND length(dst) BETWEEN 3 AND 4"
                    " AND date >= ?"
                    " AND (dep_ts IS NULL OR arr_ts IS NULL"
                    "      OR arr_ts - dep_ts >= 600)", (cutoff,)):
                m = _local_min(dep_ts, tz_by_code.get(org), zones)
                am = _local_min(dep_arr, tz_by_code.get(dst), zones)
                yield (cs.strip().upper(), org, dst,
                       (m // 5) * 5 if m is not None else None,
                       (am // 5) * 5 if am is not None else None, typ)

        for r in rows():
            batch.append(r)
            if len(batch) >= CHUNK:
                work.executemany("INSERT INTO s VALUES (?,?,?,?,?,?)", batch)
                batch = []
        if batch:
            work.executemany("INSERT INTO s VALUES (?,?,?,?,?,?)", batch)
        work.commit()
        work.execute("CREATE INDEX ix ON s (callsign, org, dst)")

        # Everything joins inside SQLite; Python holds one insert batch,
        # never a per-leg dict — memory stays flat however deep the
        # artifact grows. Mode = the most-seen 5-minute slot, ties to
        # the earliest, deterministically.
        session.execute(delete(RefSchedule))
        session.flush()
        rows_written, out = 0, []
        final = work.execute("""
            WITH base AS (
              SELECT callsign, org, dst, count(*) n
              FROM s GROUP BY callsign, org, dst),
            depm AS (
              SELECT callsign, org, dst, dep5, ROW_NUMBER() OVER (
                PARTITION BY callsign, org, dst
                ORDER BY count(*) DESC, dep5) rn
              FROM s WHERE dep5 IS NOT NULL
              GROUP BY callsign, org, dst, dep5),
            arrm AS (
              SELECT s.callsign, s.org, s.dst, s.arr5,
                ROW_NUMBER() OVER (
                  PARTITION BY s.callsign, s.org, s.dst
                  ORDER BY s.arr5) rn,
                COUNT(*) OVER (
                  PARTITION BY s.callsign, s.org, s.dst) cnt
              FROM s JOIN depm dm
                ON dm.callsign = s.callsign AND dm.org = s.org
               AND dm.dst = s.dst AND dm.rn = 1
              WHERE s.arr5 IS NOT NULL AND s.dep5 IS NOT NULL
                AND abs(s.dep5 - dm.dep5) <= 10),
            typem AS (
              SELECT callsign, org, dst, type, ROW_NUMBER() OVER (
                PARTITION BY callsign, org, dst
                ORDER BY count(*) DESC, type) rn
              FROM s WHERE type IS NOT NULL AND type <> ''
              GROUP BY callsign, org, dst, type)
            SELECT b.callsign, b.org, b.dst, b.n, d.dep5, a.arr5, t.type
            FROM base b
            LEFT JOIN depm d ON d.callsign = b.callsign AND d.org = b.org
              AND d.dst = b.dst AND d.rn = 1
            LEFT JOIN arrm a ON a.callsign = b.callsign AND a.org = b.org
              AND a.dst = b.dst AND a.rn = (a.cnt + 1) / 2
            LEFT JOIN typem t ON t.callsign = b.callsign AND t.org = b.org
              AND t.dst = b.dst AND t.rn = 1
        """)
        for cs, org, dst, n, dep5, arr5, typ in final:
            prefix = cs[:3]
            out.append({
                "callsign": cs[:12], "org": org, "dst": dst,
                "airline_icao": prefix if prefix.isalpha() else "",
                "dep_min": dep5, "arr_min": arr5,
                "type_code": typ, "n_flights": n})
            if len(out) >= CHUNK:
                session.execute(insert(RefSchedule), out)
                rows_written += len(out)
                out = []
    finally:
        src.close()
        work.close()
        os.unlink(tmp)

    if out:
        session.execute(insert(RefSchedule), out)
        rows_written += len(out)
    return rows_written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source",
                        choices=["airports", "airport_tz", "tar1090",
                                 "airframes", "seed", "routes", "leg_stats",
                                 "schedule", "airline_names", "alliances",
                                 "boards", "derive"])
    parser.add_argument("path", nargs="?",
                        help="input file (not used by 'derive')")
    parser.add_argument("--db", default=None,
                        help="database URL override (default: settings)")
    args = parser.parse_args(argv)
    settings = Settings()
    if args.source in ("leg_stats", "schedule") and not args.path:
        args.path = settings.legs_path
    if args.source == "boards" and not args.path:
        args.path = "data/boards.db"          # default to the artifact drop
    if args.source not in ("derive",) and not args.path:
        parser.error("%s needs an input file" % args.source)

    session = make_sessionmaker(args.db or settings.database_url)()
    try:
        if args.source == "derive":
            rows = derive(session)
        else:
            handler = {"airports": ingest_airports,
                       "airport_tz": ingest_airport_tz,
                       "tar1090": ingest_tar1090,
                       "airframes": ingest_airframes,
                       "seed": ingest_seed,
                       "routes": ingest_routes,
                       "airline_names": ingest_airline_names,
                       "alliances": ingest_alliances,
                       "boards": ingest_boards,
                       "leg_stats": ingest_leg_stats,
                       "schedule": ingest_schedule}[args.source]
            rows = handler(session, args.path)
        session.add(RefImport(source=args.source, rows=rows))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    print("%s: %d rows" % (args.source, rows))


if __name__ == "__main__":
    sys.exit(main())
