"""Search: one box, four kinds of thing. Registrations and hexes from
the airframe registry, flight numbers from the legs artifact, airports
and airlines from the reference tables. Prefix matching only, a few
results per kind, ranked so that an exact code comes first. Nothing
here is a new source of truth; it is the index over what the other
routes already serve.
"""
from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import case, func, or_, select

from . import openapi as spec
from . import ratelimit
from .db import get_session
from .errors import ApiError
from .refdata_models import RefAirframe, RefAirline, RefAirport, RefType

router = APIRouter(tags=["Reference"])

CACHE = "public, s-maxage=600"
PER_KIND = 5


def _norm(q: str) -> str:
    return " ".join(q.strip().upper().split())


def _shape(q: str) -> dict:
    """What the query can be, from its letters alone, so each kind runs
    only when it could match and the likeliest kind answers first."""
    compact = q.replace(" ", "").replace("-", "")
    has_digit = any(c.isdigit() for c in compact)
    alpha = compact.isalpha()
    return {
        # registrations carry a digit or a dash; a short code may be one
        "aircraft": has_digit or "-" in q or len(compact) <= 6,
        # flight numbers: two or three letters then digits, or a bare
        # airline prefix
        "flight": (len(compact) >= 3 and compact[:2].isalpha()
                   and (has_digit or len(compact) <= 4)),
        # airports and airlines are words or short codes
        "airport": alpha and 2 <= len(compact) <= 40,
        "airline": alpha and 2 <= len(compact) <= 40,
        # a word of four letters or more is a place or a name first
        "words_first": alpha and len(compact) >= 4,
    }


def _serialize_frame(frame, type_name, airline):
    bits = [type_name or frame.type_code, airline or frame.operator_name]
    return {"kind": "aircraft", "id": frame.hex,
            "label": frame.registration or frame.hex.upper(),
            "detail": " · ".join(b for b in bits if b) or None}


def _frames(session, cond):
    return session.execute(
        select(RefAirframe, RefType.name, RefAirline.name)
        .outerjoin(RefType, RefType.designator == RefAirframe.type_code)
        .outerjoin(RefAirline, RefAirline.icao == RefAirframe.operator_icao)
        .where(cond)
        .order_by(RefAirframe.registration)
        .limit(PER_KIND)).all()


def _aircraft(session, q):
    """Registrations are stored uppercase, so the prefix compares raw and
    the two indexes (registration, registration without dashes) serve
    each query on their own; two small ordered scans beat one OR."""
    rows = _frames(session, RefAirframe.registration.like(q + "%"))
    bare = q.replace("-", "")
    # Typed without the dash (9VSHA): only once a digit is in it, so a
    # city name like Doha does not surface D-OHAR.
    if "-" not in q and any(c.isdigit() for c in bare) and len(rows) < PER_KIND:
        rows += _frames(session, func.replace(RefAirframe.registration,
                                              "-", "").like(bare + "%"))
    if len(q) >= 3 and all(c in "0123456789ABCDEF" for c in q) \
            and len(rows) < PER_KIND:
        rows += _frames(session, RefAirframe.hex.like(q.lower() + "%"))
    out, seen = [], set()
    for frame, type_name, airline in rows:
        if frame.hex in seen:
            continue
        seen.add(frame.hex)
        out.append(_serialize_frame(frame, type_name, airline))
    return out[:PER_KIND]


def _flights(session, request, q):
    book = request.app.state.legs
    if not book.available():
        return []
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
        for r in book.callsigns(p, PER_KIND):
            if r["callsign"] in seen:
                continue
            seen.add(r["callsign"])
            out.append({"kind": "flight", "id": r["callsign"],
                        "label": r["callsign"],
                        "detail": (str(r["flights"]) + " flights, last "
                                   + r["last"]) if r["last"] else None})
    return out[:PER_KIND]


def _airports(session, q):
    exact = or_(RefAirport.iata == q, RefAirport.ident == q)
    names = or_(func.upper(RefAirport.name).like(q + "%"),
                func.upper(RefAirport.name).like("% " + q + "%"),
                func.upper(RefAirport.municipality).like(q + "%"))
    # case(), not exact.desc(): a NULL iata makes the OR NULL, and NULL
    # sorts first in DESC on Postgres, which put an air base above Changi.
    rows = session.execute(
        select(RefAirport)
        .where(or_(exact, names))
        .where(RefAirport.kind.in_(("large_airport", "medium_airport")))
        .order_by(case((exact, 0), else_=1),
                  case((RefAirport.iata.is_(None), 1), else_=0),
                  RefAirport.kind, RefAirport.name)
        .limit(PER_KIND)).scalars().all()
    out = []
    for a in rows:
        code = a.iata or a.ident
        detail = " · ".join(b for b in (a.municipality, a.iso_country) if b)
        out.append({"kind": "airport", "id": code,
                    "label": (a.name or code) + " (" + code + ")",
                    "detail": detail or None})
    return out


def _airlines(session, q):
    exact = or_(RefAirline.icao == q, RefAirline.iata == q)
    rows = session.execute(
        select(RefAirline)
        .where(or_(exact, func.upper(RefAirline.name).like(q + "%"),
                   func.upper(RefAirline.name).like("% " + q + "%")))
        .order_by(case((exact, 0), else_=1),
                  case((RefAirline.iata.is_(None), 1), else_=0),
                  RefAirline.name)
        .limit(PER_KIND)).scalars().all()
    return [{"kind": "airline", "id": a.icao, "label": a.name,
             "detail": " · ".join(b for b in (a.icao, a.iata) if b)}
            for a in rows]


@router.get(
    "/v1/search", summary="Search",
    description="One box over registrations, hexes, flight numbers, "
                "airports and airlines. Prefix match, up to five per kind, "
                "exact codes first. Rate: 600 per 600 s (bucket `search`). "
                "Cache: 10 min edge.",
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
    parts = {
        "aircraft": _aircraft(session, q) if shape["aircraft"] else [],
        "flight": _flights(session, request, q) if shape["flight"] else [],
        "airport": _airports(session, q) if shape["airport"] else [],
        "airline": _airlines(session, q) if shape["airline"] else [],
    }
    order = (("airport", "airline", "flight", "aircraft")
             if shape["words_first"]
             else ("aircraft", "flight", "airport", "airline"))
    results = [r for kind in order for r in parts[kind]]
    response.headers["Cache-Control"] = CACHE
    return {"q": q, "results": results}
