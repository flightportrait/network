"""Search: one box, four kinds of thing. Registrations and hexes from
the airframe registry, flight numbers from the schedule, airports and
airlines from the reference tables. Every hit carries a score (an exact
code first, then a prefix, then a word start, then a near miss, each
lifted by traffic) and the list is one ranked list, not four buckets.
Nothing here is a new source of truth; it is the index over what the
other routes already serve.

What is typed is read before it is searched: a flight number typed with
a space ("SQ 322") is one flight number, and two places joined by "to",
"from", a dash or an arrow ("Singapore to London", "SIN-LHR") are a
route. Names and cities compare without their accents ("Sao Paulo"
finds São Paulo); labels keep them.

The query has one canonical spelling, the one `q` echoes (trimmed, one
space between words, uppercase); any other is answered with a redirect
to it, so the edge keeps one cached answer per query, not one per
spelling.
"""
import math
import re
from urllib.parse import quote, unquote_plus

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import String, and_, case, func, or_, select, text

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

# Accents off, one letter for one letter (Postgres's translate() takes
# no more), over what upper() left: networkd folds with the same table.
FOLD_FROM = (
    "ÀÁÂÃÄÅÇÈÉÊËÌÍÎÏÑÒÓÔÕÖÙÚÛÜÝàáâã"
    "äåçèéêëìíîïñòóôõöùúûüýÿĀāĂăĄąĆ"
    "ćĈĉĊċČčĎďĒēĔĕĖėĘęĚěĜĝĞğĠġĢģĤĥĨ"
    "ĩĪīĬĭĮįİĴĵĶķĹĺĻļĽľŃńŅņŇňŌōŎŏŐő"
    "ŔŕŖŗŘřŚśŜŝŞşŠšŢţŤťŨũŪūŬŭŮůŰűŲų"
    "ŴŵŶŷŸŹźŻżŽžƠơƯưǍǎǏǐǑǒǓǔǕǖǗǘǙǚǛ"
    "ǜǞǟǠǡǦǧǨǩǪǫǬǭǰǴǵǸǹǺǻȀȁȂȃȄȅȆȇȈȉ"
    "ȊȋȌȍȎȏȐȑȒȓȔȕȖȗȘșȚțȞȟȦȧȨȩȪȫȬȭȮȯ"
    "ȰȱȲȳØøĐđŁłĦħŦŧı")
FOLD_TO = (
    "AAAAAACEEEEIIIINOOOOOUUUUYAAAA"
    "AACEEEEIIIINOOOOOUUUUYYAAAAAAC"
    "CCCCCCCDDEEEEEEEEEEGGGGGGGGHHI"
    "IIIIIIIIJJKKLLLLLLNNNNNNOOOOOO"
    "RRRRRRSSSSSSSSTTTTUUUUUUUUUUUU"
    "WWYYYZZZZZZOOUUAAIIOOUUUUUUUUU"
    "UAAAAGGKKOOOOJGGNNAAAAAAEEEEII"
    "IIOOOORRRRUUUUSSTTHHAAEEOOOOOO"
    "OOYYOODDLLHHTTI")
_FOLD = str.maketrans(FOLD_FROM, FOLD_TO)

# A flight number typed with a space: an airline code of two or three
# letters and digits, then the number (and a letter, if it has one).
_SPACED_FLIGHT = re.compile(r"([A-Z0-9]{2,3}) ([0-9]{1,4}[A-Z]?)", re.ASCII)
ARROW = "\u2192"
_ARROWS = ("->", ARROW, "\u2013", "\u2014", ">")
_JOINS = ("TO", "FROM", ARROW)


def _norm(q: str) -> str:
    return " ".join(q.strip().upper().split())


def _fold(s: str) -> str:
    return s.translate(_FOLD)


def _folded(col):
    """upper(col) without its accents, in SQL."""
    return func.translate(func.upper(col), FOLD_FROM, FOLD_TO,
                          type_=String)


def _whole(text_, q) -> bool:
    """q is one of the words of text_ (both folded)."""
    return bool(text_) and (" " + q + " ") in (" " + text_ + " ")


def _compact_flight(q: str) -> str:
    """"SQ 322" -> "SQ322"; anything else as it is."""
    m = _SPACED_FLIGHT.fullmatch(q)
    if m and not m.group(1).isdigit():
        return m.group(1) + m.group(2)
    return q


def _places(q: str):
    """A route asked in words: (from, to), or None. "SIN LHR", "SIN-LHR",
    "SIN → LHR", "Singapore to London", "from SIN to LHR", "flights to
    London from Singapore". A dash joins two places only between words
    of three letters or more, so 9V-SMA stays a registration."""
    s = q
    for a in _ARROWS:
        s = s.replace(a, " " + ARROW + " ")
    if "-" in s:
        left, _, right = s.partition("-")
        lt = [t for t in left.split(" ") if t]
        rt = [t for t in right.split(" ") if t]
        if "-" not in right and lt and rt \
                and all(t.isalpha() for t in lt + rt) \
                and len(lt[-1]) >= 3 and len(rt[0]) >= 3:
            s = left + " " + ARROW + " " + right
    tokens = [t for t in s.split(" ") if t]
    if len(tokens) > 1 and tokens[0] == "FLIGHTS":
        tokens = tokens[1:]
    if not tokens:
        return None

    def side(ts):
        if not ts or any(t in _JOINS for t in ts):
            return None
        return " ".join(ts)

    a = b = None
    if ARROW in tokens:
        i = tokens.index(ARROW)
        left = tokens[:i]
        if left and left[0] == "FROM":
            left = left[1:]
        a, b = side(left), side(tokens[i + 1:])
    elif tokens[0] == "FROM" and "TO" in tokens:
        i = tokens.index("TO")
        a, b = side(tokens[1:i]), side(tokens[i + 1:])
    elif tokens[0] == "TO" and "FROM" in tokens:
        i = tokens.index("FROM")
        b, a = side(tokens[1:i]), side(tokens[i + 1:])
    elif "TO" in tokens:
        i = tokens.index("TO")
        a, b = side(tokens[:i]), side(tokens[i + 1:])
    elif len(tokens) == 2:
        a, b = side(tokens[:1]), side(tokens[1:])
    return (a, b) if a and b else None


def _canonical_url(request: Request, q: str) -> str:
    """This request with q in its canonical spelling, every other
    parameter as it came."""
    parts, placed = [], False
    for piece in request.url.query.split("&"):
        if not piece:
            continue
        if unquote_plus(piece.split("=", 1)[0]) == "q":
            if not placed:
                parts.append("q=" + quote(q, safe=""))
                placed = True
            continue
        parts.append(piece)
    return request.url.path + "?" + "&".join(parts)


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
                _folded(RefAirline.name).like(_fold(airline_q) + "%")))
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


def _schedule_rows(session, prefix):
    """The numbers starting with prefix: the prefix itself first, if it
    is one, then the busiest."""
    return session.execute(
        select(RefSchedule.callsign, func.sum(RefSchedule.n_flights),
               func.min(RefSchedule.org), func.min(RefSchedule.dst),
               func.count())
        .where(RefSchedule.callsign.like(prefix + "%"))
        .group_by(RefSchedule.callsign)
        .order_by(case((RefSchedule.callsign == prefix, 0), else_=1),
                  func.sum(RefSchedule.n_flights).desc(),
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
        for callsign, n, org, dst, legs in _schedule_rows(session, p):
            # a bare prefix flown as a callsign ("CDG") is noise
            if callsign in seen or not any(c.isdigit() for c in callsign):
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
    # every candidate scored, then the best five: BAW16 (exact) is not
    # cut by five busier BA16… rows read first
    out.sort(key=lambda r: -r["score"])
    return out[:PER_KIND]


def _airport_code(session, word):
    """A code, or a city or airport name, to one airport code. Cities
    with several airports resolve to the busiest; a busy large airport
    answers for a word of its name too (Changi, Heathrow)."""
    fw = _fold(word)
    name, city = _folded(RefAirport.name), _folded(RefAirport.municipality)
    traffic = _traffic(RefAirport)
    exact = or_(RefAirport.iata == word, RefAirport.ident == word)
    row = session.execute(
        select(RefAirport).where(or_(
            exact, city.like(fw + "%"), name.like(fw + "%"),
            _busy(name.like("% " + fw + "%"), traffic)))
        .where(RefAirport.kind.in_(("large_airport", "medium_airport")))
        .order_by(case((exact, 0), else_=1), traffic.desc())
        .limit(1)).scalar()
    return (row.iata or row.ident) if row else None


def _busy(word, traffic):
    """A word start in the name of a large airport the network sees
    flights from: it ranks as a prefix. Changi is SIN before the air
    base whose name starts with it."""
    return and_(word, RefAirport.kind == "large_airport", traffic > 0)


def _route(session, a, b):
    """Two places: the flight numbers between them. "SIN LHR",
    "Singapore to London"."""
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
    """(hits, whether q is a whole code or word of one of them)."""
    fq = _fold(q)
    name, city = _folded(RefAirport.name), _folded(RefAirport.municipality)
    traffic = _traffic(RefAirport)
    exact = or_(RefAirport.iata == q, RefAirport.ident == q)
    starts = or_(name.like(fq + "%"), city.like(fq + "%"))
    word = name.like("% " + fq + "%")
    cls = case((exact, EXACT), (starts, PREFIX),
               (_busy(word, traffic), PREFIX), else_=WORD)
    rows = session.execute(
        select(RefAirport, cls, traffic, name, city)
        .where(or_(exact, starts, word))
        .where(RefAirport.kind.in_(("large_airport", "medium_airport")))
        .order_by(cls.desc(), traffic.desc(),
                  case((RefAirport.iata.is_(None), 1), else_=0),
                  RefAirport.name)
        .limit(PER_KIND)).all()
    whole = any(score == EXACT or _whole(n, fq) or _whole(c, fq)
                for _, score, _, n, c in rows)
    return [_airport_item(a, score, t) for a, score, t, _, _ in rows], whole


def _airports_near(session, q):
    """A near miss: Chnagi, Heathro. Edit distance against each word of
    the name and city (one edit for four letters, two from five); trigrams
    would rank Chicago above Changi for a transposition."""
    fq = _fold(q)
    near = text(
        "EXISTS (SELECT 1 FROM regexp_split_to_table("
        " translate(upper(coalesce(ref_airports.name, '')), :f, :t) || ' ' ||"
        " translate(upper(coalesce(ref_airports.municipality, '')), :f, :t),"
        " '\\s+') w WHERE length(w) >= 4 AND levenshtein(w, :q) <= :d)"
    ).bindparams(q=fq, d=_edits(q), f=FOLD_FROM, t=FOLD_TO)
    traffic = _traffic(RefAirport)
    rows = session.execute(
        select(RefAirport, text(str(NEAR)), traffic)
        .where(near)
        .where(RefAirport.kind.in_(("large_airport", "medium_airport")))
        .order_by(traffic.desc())
        .limit(PER_KIND)).all()
    return [_airport_item(a, score, t) for a, score, t in rows]


def _airport_item(a, score, traffic_n):
    code = a.iata or a.ident
    detail = " · ".join(b for b in (a.municipality, a.iso_country) if b)
    return {"kind": "airport", "id": code,
            "label": (a.name or code) + " (" + code + ")",
            "detail": detail or None,
            "score": score + _lift(traffic_n) + (2 if a.iata else 0)}


def _edits(q):
    return 1 if len(q) == 4 else 2


def _airlines(session, q):
    """(hits, whether q is a whole code or word of one of them)."""
    fq = _fold(q)
    name = _folded(RefAirline.name)
    exact = or_(RefAirline.icao == q, RefAirline.iata == q)
    starts = name.like(fq + "%")
    word = name.like("% " + fq + "%")
    cls = case((exact, EXACT), (starts, PREFIX), else_=WORD)
    rows = session.execute(
        select(RefAirline, cls, name)
        .where(or_(exact, starts, word))
        .order_by(cls.desc(),
                  case((RefAirline.iata.is_(None), 1), else_=0),
                  RefAirline.name)
        .limit(PER_KIND)).all()
    whole = any(score == EXACT or _whole(n, fq) for _, score, n in rows)
    return [_airline_item(a, score) for a, score, _ in rows], whole


def _airlines_near(session, q):
    near = text(
        "EXISTS (SELECT 1 FROM regexp_split_to_table("
        " translate(upper(ref_airlines.name), :f, :t), '\\s+') w"
        " WHERE length(w) >= 4 AND levenshtein(w, :q) <= :d)"
    ).bindparams(q=_fold(q), d=_edits(q), f=FOLD_FROM, t=FOLD_TO)
    rows = session.execute(
        select(RefAirline, text(str(NEAR)))
        .where(near).order_by(RefAirline.name).limit(PER_KIND)).all()
    return [_airline_item(a, score) for a, score in rows]


def _airline_item(a, score):
    return {"kind": "airline", "id": a.icao, "label": a.name,
            "detail": " · ".join(b for b in (a.icao, a.iata) if b),
            "score": score + (2 if a.iata else 0)}


@router.get(
    "/v1/search", summary="Search",
    description="One box over registrations, hexes, flight numbers "
                "(\"SQ322\", \"SQ 322\"), routes (two places: \"SIN LHR\", "
                "\"SIN-LHR\", \"Singapore to London\"), fleets (an operator "
                "and a type), airports and airlines; names compare without "
                "accents. One ranked list: exact codes, then prefixes, then "
                "word starts, then near misses (only when nothing matched "
                "a whole code or word), traffic as the tie-breaker. The "
                "canonical query is trimmed, one space between words, "
                "uppercase; any other spelling is answered 301 to it. "
                "Rate: 600 per 600 s (bucket `search`). Cache: 12 h edge.",
    operation_id="search",
    responses=spec.ok(spec.EX_SEARCH, spec.R301_SEARCH, spec.R429,
                      schema=spec.SCH_SEARCH),
    openapi_extra=spec.MAP_TIER,
)
def search(request: Request, response: Response,
           q: str = Query(..., min_length=1, max_length=40,
                          description="What was typed."),
           session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.search_rate_limit,
                       settings.rate_window_s, bucket="search")
    raw, q = q, _norm(q)
    if len(q) < 2:
        raise ApiError(422, "invalid_request", "type at least two characters")
    if raw != q:
        return Response(status_code=301, headers={
            "Location": _canonical_url(request, q), "Cache-Control": CACHE})
    term = _compact_flight(q)
    shape = _shape(term)
    places = _places(term)
    results = []
    if places:
        results += _route(session, *places)
    if shape["pair"]:
        results += _fleet(session, term)
    if shape["aircraft"]:
        results += _aircraft(session, term)
    if shape["flight"]:
        results += _flights(session, request, term)
    airports, whole_airport = _airports(session, term) \
        if shape["airport"] else ([], False)
    airlines, whole_airline = _airlines(session, term) \
        if shape["airline"] else ([], False)
    results += airports + airlines
    # near misses only when nothing matched a whole code or word: Scoot
    # the airline, not Scott AFB as well
    if len(term) >= 4 and term.isalpha() \
            and session.bind.dialect.name == "postgresql" \
            and not whole_airport and not whole_airline \
            and not any(r["score"] >= EXACT for r in results):
        if shape["airport"] and not airports:
            results += _airports_near(session, term)
        if shape["airline"] and not airlines:
            results += _airlines_near(session, term)
    # a stable sort keeps each kind's own order (its SQL tie-breakers)
    # for equal scores
    results.sort(key=lambda r: (-r["score"], KIND_ORDER[r["kind"]]))
    for r in results:
        r["score"] = round(r["score"], 1)
    response.headers["Cache-Control"] = CACHE
    return {"q": q, "results": results}
