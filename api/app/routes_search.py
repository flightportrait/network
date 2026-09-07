"""Search: one box, four kinds of thing. Registrations and hexes from
the airframe registry, flight numbers from the schedule, airports and
airlines from the reference tables. Every hit carries a score (an exact
code first, then a prefix, then a word start, then a near miss, each
lifted by traffic) and the list is one ranked list, not four buckets.
Nothing here is a new source of truth; it is the index over what the
other routes already serve.
"""
import math
import re

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import case, func, or_, select, text

from . import openapi as spec
from . import ratelimit
from .db import get_session
from .errors import ApiError
from .refdata_models import (RefAirframe, RefAirline, RefAirport,
                             RefSchedule, RefType)

router = APIRouter(tags=["Reference"])

# The data behind search changes once a night.
CACHE = "public, s-maxage=43200"
PER_KIND = 5
EXACT, PREFIX, WORD, NEAR = 100, 60, 40, 20
KIND_ORDER = {"flight": 0, "aircraft": 1, "airport": 2, "airline": 3}


def _norm(q: str) -> str:
    return " ".join(q.strip().upper().split())


def _lift(n) -> float:
    """Traffic as a tie-breaker inside a match class, never across."""
    return math.log10(1 + (n or 0)) * 4


def _shape(q: str) -> dict:
    compact = q.replace(" ", "").replace("-", "")
    has_digit = any(c.isdigit() for c in compact)
    alpha = compact.isalpha()
    tokens = q.split(" ")
    return {
        "aircraft": has_digit or "-" in q or len(compact) <= 6,
        "flight": (len(compact) >= 3 and compact[:2].isalpha()
                   and (has_digit or len(compact) <= 4) and len(tokens) == 1),
        "airport": alpha and 2 <= len(compact) <= 40,
        "airline": alpha and 2 <= len(compact) <= 40,
        "pair": len(tokens) == 2,
    }


def _serialize_frame(frame, type_name, airline, score):
    bits = [type_name or frame.type_code, airline or frame.operator_name]
    return {"kind": "aircraft", "id": frame.hex,
            "label": frame.registration or frame.hex.upper(),
            "detail": " · ".join(b for b in bits if b) or None,
            "score": score}


def _frames(session, cond, limit=PER_KIND):
    return session.execute(
        select(RefAirframe, RefType.name, RefAirline.name)
        .outerjoin(RefType, RefType.designator == RefAirframe.type_code)
        .outerjoin(RefAirline, RefAirline.icao == RefAirframe.operator_icao)
        .where(cond)
        .order_by(RefAirframe.registration)
        .limit(limit)).all()


def _aircraft(session, q):
    """Registrations are stored uppercase, so the prefix compares raw and
    the two indexes (registration, registration without dashes) serve
    each query on their own."""
    rows = [(r, EXACT if r[0].registration == q else PREFIX)
            for r in _frames(session, RefAirframe.registration.like(q + "%"))]
    bare = q.replace("-", "")
    # Typed without the dash (9VSHA): only once a digit is in it, so a
    # city name like Doha does not surface D-OHAR.
    if "-" not in q and any(c.isdigit() for c in bare) and len(rows) < PER_KIND:
        rows += [(r, PREFIX) for r in _frames(
            session, func.replace(RefAirframe.registration, "-", "")
            .like(bare + "%"))]
    if len(q) >= 3 and all(c in "0123456789ABCDEF" for c in q) \
            and len(rows) < PER_KIND:
        rows += [(r, EXACT if r[0].hex == q.lower() else PREFIX) for r in
                 _frames(session, RefAirframe.hex.like(q.lower() + "%"))]
    out, seen = [], set()
    for (frame, type_name, airline), score in rows:
        if frame.hex in seen:
            continue
        seen.add(frame.hex)
        out.append(_serialize_frame(frame, type_name, airline, score))
    return out[:PER_KIND]


def _fleet(session, q):
    """Two words, an operator and a type: "Qatar A350", "SIA 777". The
    registry's observed operator and type answer, busiest types first
    by nothing more than registration order."""
    a, b = q.split(" ")
    for airline_q, type_q in ((a, b), (b, a)):
        airline = session.execute(
            select(RefAirline).where(or_(
                RefAirline.icao == airline_q, RefAirline.iata == airline_q,
                func.upper(RefAirline.name).like(airline_q + "%")))
            .order_by(RefAirline.name).limit(1)).scalar()
        if airline is None:
            continue
        types = session.execute(
            select(RefType.designator).where(or_(
                RefType.designator == type_q,
                func.upper(RefType.name).like("%" + type_q + "%")))
            .limit(20)).scalars().all()
        if not types:
            continue
        # observed operator first; the registry's own operator name
        # for airframes the network has not watched fly yet
        who = or_(RefAirframe.operator_icao == airline.icao,
                  RefAirframe.operator_norm.like(airline.name.upper() + "%"))
        rows = _frames(session, who & RefAirframe.type_code.in_(types))
        return [_serialize_frame(f, t, al, WORD) for f, t, al in rows]
    return []


def _schedule_rows(session, cond):
    return session.execute(
        select(RefSchedule.callsign, func.sum(RefSchedule.n_flights),
               func.min(RefSchedule.org), func.min(RefSchedule.dst),
               func.count())
        .where(cond)
        .group_by(RefSchedule.callsign)
        .order_by(func.sum(RefSchedule.n_flights).desc(),
                  RefSchedule.callsign)
        .limit(PER_KIND)).all()


def _flight_item(callsign, n, org, dst, legs, base):
    route = (org + " \u2192 " + dst) if legs == 1 else (str(legs) + " legs")
    return {"kind": "flight", "id": callsign, "label": callsign,
            "detail": route + " · " + str(n) + " flights",
            "score": base + _lift(n)}


def _flights(session, request, q):
    """Flight numbers from the inferred schedule (one row per number and
    leg, with its count), so a bare airline prefix costs an index walk
    and not an aggregation over every leg the airline ever flew. The
    legs artifact fills in only for a specific number the schedule has
    not kept (a one-off) once the query carries a digit."""
    prefix = q.replace(" ", "")
    prefixes = [prefix]
    # An IATA flight number (SQ322) is also its ICAO callsign (SIA322).
    if prefix[2].isdigit():
        icao = session.execute(
            select(RefAirline.icao).where(RefAirline.iata == prefix[:2])
        ).scalar()
        if icao:
            prefixes.append(icao + prefix[2:])
    out, seen = [], set()
    for p in prefixes:
        for callsign, n, org, dst, legs in _schedule_rows(
                session, RefSchedule.callsign.like(p + "%")):
            if callsign in seen:
                continue
            seen.add(callsign)
            out.append(_flight_item(callsign, n, org, dst, legs,
                                    EXACT if callsign == p else PREFIX))
    book = request.app.state.legs
    if len(out) < PER_KIND and any(c.isdigit() for c in prefix) \
            and book.available():
        for p in prefixes:
            for r in book.callsigns(p, PER_KIND):
                if r["callsign"] in seen:
                    continue
                seen.add(r["callsign"])
                out.append({"kind": "flight", "id": r["callsign"],
                            "label": r["callsign"],
                            "detail": (str(r["flights"]) + " flights, last "
                                       + r["last"]) if r["last"] else None,
                            "score": (EXACT if r["callsign"] == p else PREFIX)
                            + _lift(r["flights"]) - 1})
    return out[:PER_KIND]


def _airport_code(session, word):
    """A code, or a city or airport name, to one airport code. Cities
    with several airports resolve to the busiest."""
    row = session.execute(
        select(RefAirport).where(or_(
            RefAirport.iata == word, RefAirport.ident == word,
            func.upper(RefAirport.municipality).like(word + "%"),
            func.upper(RefAirport.name).like(word + "%")))
        .where(RefAirport.kind.in_(("large_airport", "medium_airport")))
        .order_by(case((or_(RefAirport.iata == word,
                            RefAirport.ident == word), 0), else_=1),
                  _traffic(RefAirport).desc())
        .limit(1)).scalar()
    return (row.iata or row.ident) if row else None


def _route(session, q):
    """Two places: the flight numbers between them. "SIN LHR",
    "Singapore London"."""
    a, b = q.split(" ")
    org, dst = _airport_code(session, a), _airport_code(session, b)
    if not org or not dst or org == dst:
        return []
    rows = session.execute(
        select(RefSchedule.callsign, RefSchedule.n_flights)
        .where(RefSchedule.org == org, RefSchedule.dst == dst)
        .order_by(RefSchedule.n_flights.desc())
        .limit(PER_KIND)).all()
    return [_flight_item(cs, n, org, dst, 1, WORD) for cs, n in rows]


def _traffic(model):
    """Observed departures from an airport: the rank a person means by
    "the London airport". Flights, not schedule rows: a business-jet
    field has many one-flight callsigns. A correlated sum over the
    (org, dst) index."""
    return (select(func.coalesce(func.sum(RefSchedule.n_flights), 0))
            .select_from(RefSchedule)
            .where(RefSchedule.org == func.coalesce(model.iata, model.ident))
            .scalar_subquery())


def _airports(session, q):
    exact = or_(RefAirport.iata == q, RefAirport.ident == q)
    starts = or_(func.upper(RefAirport.name).like(q + "%"),
                 func.upper(RefAirport.municipality).like(q + "%"))
    word = func.upper(RefAirport.name).like("% " + q + "%")
    cls = case((exact, EXACT), (starts, PREFIX), else_=WORD)
    traffic = _traffic(RefAirport)
    rows = session.execute(
        select(RefAirport, cls, traffic)
        .where(or_(exact, starts, word))
        .where(RefAirport.kind.in_(("large_airport", "medium_airport")))
        .order_by(cls.desc(), traffic.desc(),
                  case((RefAirport.iata.is_(None), 1), else_=0),
                  RefAirport.name)
        .limit(PER_KIND)).all()
    if not rows and session.bind.dialect.name == "postgresql" \
            and len(q) >= 5 and q.isalpha():
        # a near miss: Chnagi, Heathro. Edit distance against each word
        # of the name and city, two edits at most; trigrams rank Chicago
        # above Changi for a transposition.
        near = text(
            "EXISTS (SELECT 1 FROM regexp_split_to_table("
            " upper(coalesce(ref_airports.name, '')) || ' ' ||"
            " upper(coalesce(ref_airports.municipality, '')), '\\s+') w"
            " WHERE length(w) >= 4 AND levenshtein(w, :q) <= 2)"
        ).bindparams(q=q)
        rows = session.execute(
            select(RefAirport, text(str(NEAR)), traffic)
            .where(near)
            .where(RefAirport.kind.in_(("large_airport", "medium_airport")))
            .order_by(traffic.desc())
            .limit(PER_KIND)).all()
    out = []
    for a, score, traffic_n in rows:
        code = a.iata or a.ident
        detail = " · ".join(b for b in (a.municipality, a.iso_country) if b)
        out.append({"kind": "airport", "id": code,
                    "label": (a.name or code) + " (" + code + ")",
                    "detail": detail or None,
                    "score": score + _lift(traffic_n) + (2 if a.iata else 0)})
    return out


def _airlines(session, q):
    exact = or_(RefAirline.icao == q, RefAirline.iata == q)
    starts = func.upper(RefAirline.name).like(q + "%")
    word = func.upper(RefAirline.name).like("% " + q + "%")
    cls = case((exact, EXACT), (starts, PREFIX), else_=WORD)
    rows = session.execute(
        select(RefAirline, cls)
        .where(or_(exact, starts, word))
        .order_by(cls.desc(),
                  case((RefAirline.iata.is_(None), 1), else_=0),
                  RefAirline.name)
        .limit(PER_KIND)).all()
    if not rows and session.bind.dialect.name == "postgresql" \
            and len(q) >= 5 and q.isalpha():
        near = text(
            "EXISTS (SELECT 1 FROM regexp_split_to_table("
            " upper(ref_airlines.name), '\\s+') w"
            " WHERE length(w) >= 4 AND levenshtein(w, :q) <= 2)"
        ).bindparams(q=q)
        rows = session.execute(
            select(RefAirline, text(str(NEAR)))
            .where(near).order_by(RefAirline.name).limit(PER_KIND)).all()
    return [{"kind": "airline", "id": a.icao, "label": a.name,
             "detail": " · ".join(b for b in (a.icao, a.iata) if b),
             "score": score + (2 if a.iata else 0)}
            for a, score in rows]


@router.get(
    "/v1/search", summary="Search",
    description="One box over registrations, hexes, flight numbers, "
                "routes (two places), fleets (an operator and a type), "
                "airports and airlines. One ranked list: exact codes, then "
                "prefixes, then word starts, then near misses, traffic as "
                "the tie-breaker. Rate: 600 per 600 s (bucket `search`). "
                "Cache: 12 h edge.",
    operation_id="search",
    responses=spec.ok(spec.EX_SEARCH, spec.R429, schema=spec.SCH_SEARCH),
    openapi_extra=spec.MAP_TIER,
)
def search(request: Request, response: Response,
           q: str = Query(..., min_length=1, max_length=40,
                          description="What was typed."),
           session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.search_rate_limit,
                       settings.rate_window_s, bucket="search")
    q = _norm(q)
    if len(q) < 2:
        raise ApiError(422, "invalid_request", "type at least two characters")
    shape = _shape(q)
    results = []
    if shape["pair"]:
        results += _route(session, q) + _fleet(session, q)
    if shape["aircraft"]:
        results += _aircraft(session, q)
    if shape["flight"]:
        results += _flights(session, request, q)
    if shape["airport"]:
        results += _airports(session, q)
    if shape["airline"]:
        results += _airlines(session, q)
    # a stable sort keeps each kind's own order (its SQL tie-breakers)
    # for equal scores
    results.sort(key=lambda r: (-r["score"], KIND_ORDER[r["kind"]]))
    for r in results:
        r["score"] = round(r["score"], 1)
    response.headers["Cache-Control"] = CACHE
    return {"q": q, "results": results}
