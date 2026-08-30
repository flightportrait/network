"""Reference routes: open per-item lookups over the ref_* tables —
airlines, alliances, types. Everything here is slow-moving open data,
so responses cache hard; a table nobody has ingested yet just 404s and
the consumer degrades — same posture as the RouteBook file.
"""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import and_, func, or_, select

from . import openapi as spec
from . import ratelimit
from .db import get_session
from .errors import ApiError
from .refdata_models import (RefAirframe, RefAirline, RefAirlineCountry,
                             RefAlliance, RefAllianceMembership, RefAirport,
                             RefLegStat, RefRoute, RefSchedule, RefType)

router = APIRouter(tags=["Reference"])

CACHE = "public, s-maxage=3600"
COVERAGE_RELATIONSHIPS = ("member", "group-brand")


def _throttle(request: Request):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.refdata_rate_limit,
                       settings.rate_window_s, bucket="refdata")


def _airline_or_404(session, icao: str) -> RefAirline:
    airline = session.get(RefAirline, icao.strip().upper())
    if airline is None:
        raise ApiError(404, "not_found", "unknown airline")
    return airline


def _iso(value):
    return value.isoformat() if value else None


def _serialize_membership(row: RefAllianceMembership,
                          alliance: RefAlliance) -> dict:
    return {
        "slug": alliance.slug, "name": alliance.name,
        "status": row.status, "relationship": row.relationship,
        "sponsor_icao": row.sponsor_icao,
        "effective_from": _iso(row.effective_from),
        "effective_to": _iso(row.effective_to),
        "source_url": row.source_url,
        "source_checked_at": _iso(row.source_checked_at),
    }


def _memberships_by_airline(session, airline_codes=None):
    query = select(RefAllianceMembership, RefAlliance).join(
        RefAlliance, RefAlliance.slug == RefAllianceMembership.alliance_slug)
    if airline_codes is not None:
        query = query.where(RefAllianceMembership.airline_icao.in_(airline_codes))
    out = {}
    for membership, alliance in session.execute(query):
        out.setdefault(membership.airline_icao, []).append(
            _serialize_membership(membership, alliance))
    for rows in out.values():
        rows.sort(key=lambda x: (x["status"] != "active", x["name"]))
    return out


def _serialize_airline(airline: RefAirline, memberships=None) -> dict:
    return {"icao": airline.icao, "iata": airline.iata,
            "name": airline.name, "palette": airline.palette or [],
            "alliances": memberships or []}


def _alliance_or_404(session, slug: str) -> RefAlliance:
    alliance = session.get(RefAlliance, slug.strip().lower())
    if alliance is None:
        raise ApiError(404, "not_found", "unknown alliance")
    return alliance


def _alliance_memberships(session, slug):
    return session.execute(
        select(RefAllianceMembership)
        .where(RefAllianceMembership.alliance_slug == slug)
        .order_by(RefAllianceMembership.status,
                  RefAllianceMembership.relationship,
                  RefAllianceMembership.airline_icao)).scalars().all()


def _coverage_codes(memberships):
    return {m.airline_icao for m in memberships
            if m.status == "active"
            and m.relationship in COVERAGE_RELATIONSHIPS}


def _alliance_summary(session, alliance, memberships=None):
    memberships = memberships or _alliance_memberships(session, alliance.slug)
    direct = {m.airline_icao for m in memberships
              if m.status == "active" and m.relationship == "member"}
    codes = _coverage_codes(memberships)
    route_counts, countries, legs = {}, set(), set()
    n_flights = 0
    n_airframes = 0
    if codes:
        route_counts = dict(session.execute(
            select(RefRoute.airline_icao, func.count())
            .where(RefRoute.airline_icao.in_(codes))
            .group_by(RefRoute.airline_icao)).all())
        countries = set(session.execute(
            select(RefAirlineCountry.iso_country)
            .where(RefAirlineCountry.airline_icao.in_(codes))).scalars())
        for o, d, n in session.execute(
                select(RefLegStat.o, RefLegStat.d, RefLegStat.n_flights)
                .where(RefLegStat.airline_icao.in_(codes))):
            legs.add((o, d))
            n_flights += n
        n_airframes = session.execute(
            select(func.count()).select_from(RefAirframe)
            .where(RefAirframe.operator_icao.in_(codes))).scalar_one()
    return {
        "slug": alliance.slug, "name": alliance.name,
        "website_url": alliance.website_url,
        "logo_asset_url": alliance.logo_asset_url,
        "source_url": alliance.source_url,
        "source_checked_at": _iso(alliance.source_checked_at),
        "n_members": len(direct),
        "n_members_observed": sum(1 for c in direct if route_counts.get(c, 0)),
        "n_airlines": len(codes),
        "n_routes": sum(route_counts.values()),
        "n_legs": len(legs), "n_flights": n_flights,
        "n_countries": len(countries), "n_airframes": n_airframes,
    }


@router.get(
    "/v1/airlines", summary="Airlines",
    description="Airlines in the reference tables, with observed route "
                "and country counts so a consumer can rank and filter in "
                "one request. Rate: 300 per 600 s (bucket `refdata`). "
                "Cache: 1 h edge.",
    operation_id="airlines",
    responses=spec.ok(spec.EX_AIRLINES, spec.R429,
                      schema=spec.SCH_AIRLINES),
    openapi_extra=spec.STABLE,
)
def airlines(request: Request, response: Response,
             session=Depends(get_session)):
    _throttle(request)
    rows = session.execute(
        select(RefAirline).order_by(RefAirline.icao)).scalars().all()
    memberships = _memberships_by_airline(session, [a.icao for a in rows])
    route_counts = dict(session.execute(
        select(RefRoute.airline_icao, func.count())
        .where(RefRoute.airline_icao.is_not(None))
        .group_by(RefRoute.airline_icao)).all())
    country_counts = dict(session.execute(
        select(RefAirlineCountry.airline_icao, func.count())
        .group_by(RefAirlineCountry.airline_icao)).all())
    response.headers["Cache-Control"] = CACHE
    out = []
    for a in rows:
        d = _serialize_airline(a, memberships.get(a.icao))
        d["n_routes"] = route_counts.get(a.icao, 0)
        d["n_countries"] = country_counts.get(a.icao, 0)
        out.append(d)
    return {"airlines": out}


@router.get(
    "/v1/airlines/{icao}", summary="Airline",
    description="One airline. n_routes is an observed lower bound. Rate: "
                "300 per 600 s (bucket `refdata`). Cache: 1 h edge.",
    operation_id="airline",
    responses=spec.ok(spec.EX_AIRLINE, spec.R429, spec.R404,
                      schema=spec.SCH_AIRLINE),
    openapi_extra=spec.STABLE,
)
def airline(icao: spec.AirlineICAO, request: Request, response: Response,
            session=Depends(get_session)):
    _throttle(request)
    row = _airline_or_404(session, icao)
    response.headers["Cache-Control"] = CACHE
    memberships = _memberships_by_airline(session, [row.icao])
    out = _serialize_airline(row, memberships.get(row.icao))
    out["n_routes"] = session.execute(
        select(func.count()).select_from(RefRoute)
        .where(RefRoute.airline_icao == row.icao)).scalar_one()
    return out


@router.get(
    "/v1/alliances", summary="Alliances",
    description="Global alliances and how much of each we have observed. "
                "Member counts are official. Route, flight, and airframe "
                "counts are observed lower bounds. Rate: 300 per 600 s "
                "(bucket `refdata`). Cache: 1 h edge.",
    operation_id="alliances",
    responses=spec.ok(spec.EX_ALLIANCES, spec.R429),
    openapi_extra=spec.MAP_TIER,
)
def alliances(request: Request, response: Response,
              session=Depends(get_session)):
    _throttle(request)
    rows = session.execute(
        select(RefAlliance).order_by(RefAlliance.name)).scalars().all()
    response.headers["Cache-Control"] = CACHE
    return {"alliances": [_alliance_summary(session, row) for row in rows]}


@router.get(
    "/v1/alliances/{slug}", summary="Alliance",
    description="One alliance: sourced memberships and observed coverage. "
                "Rate: 300 per 600 s (bucket `refdata`). Cache: 1 h edge.",
    operation_id="alliance",
    responses={**spec.R429, **spec.R404},
    openapi_extra=spec.MAP_TIER,
)
def alliance(slug: spec.AllianceSlug, request: Request, response: Response,
             session=Depends(get_session)):
    _throttle(request)
    row = _alliance_or_404(session, slug)
    memberships = _alliance_memberships(session, row.slug)
    codes = [m.airline_icao for m in memberships]
    airlines_by_code = {a.icao: a for a in session.execute(
        select(RefAirline).where(RefAirline.icao.in_(codes))).scalars()}
    route_counts = dict(session.execute(
        select(RefRoute.airline_icao, func.count())
        .where(RefRoute.airline_icao.in_(codes))
        .group_by(RefRoute.airline_icao)).all()) if codes else {}
    country_counts = dict(session.execute(
        select(RefAirlineCountry.airline_icao, func.count())
        .where(RefAirlineCountry.airline_icao.in_(codes))
        .group_by(RefAirlineCountry.airline_icao)).all()) if codes else {}
    members = []
    for membership in memberships:
        airline_row = airlines_by_code.get(membership.airline_icao)
        members.append({
            "airline": _serialize_airline(airline_row) if airline_row else {
                "icao": membership.airline_icao, "iata": None,
                "name": membership.airline_icao, "palette": [],
                "alliances": []},
            "status": membership.status,
            "relationship": membership.relationship,
            "sponsor_icao": membership.sponsor_icao,
            "effective_from": _iso(membership.effective_from),
            "effective_to": _iso(membership.effective_to),
            "source_url": membership.source_url,
            "source_checked_at": _iso(membership.source_checked_at),
            "note": membership.note,
            "n_routes": route_counts.get(membership.airline_icao, 0),
            "n_countries": country_counts.get(membership.airline_icao, 0),
        })

    country_rollup = {}
    active_codes = _coverage_codes(memberships)
    if active_codes:
        for country, n in session.execute(
                select(RefAirlineCountry.iso_country,
                       func.sum(RefAirlineCountry.n_routes))
                .where(RefAirlineCountry.airline_icao.in_(active_codes))
                .group_by(RefAirlineCountry.iso_country)):
            country_rollup[country] = n
    response.headers["Cache-Control"] = CACHE
    return {
        "alliance": _alliance_summary(session, row, memberships),
        "memberships": members,
        "countries": [{"iso_country": c, "n_routes": n}
                      for c, n in sorted(country_rollup.items(),
                                         key=lambda x: (-x[1], x[0]))],
    }


@router.get(
    "/v1/alliances/{slug}/routes", summary="Alliance routes",
    description="Observed legs across current member brands, undirected "
                "(org/dst in canonical alphabetical order). Rate: 300 per "
                "600 s (bucket `refdata`). Cache: 1 h edge.",
    operation_id="alliance_routes",
    responses={**spec.R429, **spec.R404},
    openapi_extra=spec.MAP_TIER,
)
def alliance_routes(slug: spec.AllianceSlug, request: Request,
                    response: Response, session=Depends(get_session)):
    _throttle(request)
    row = _alliance_or_404(session, slug)
    memberships = _alliance_memberships(session, row.slug)
    codes = _coverage_codes(memberships)
    stats = session.execute(
        select(RefLegStat).where(RefLegStat.airline_icao.in_(codes))
    ).scalars().all() if codes else []
    leg_map, airport_codes = {}, set()
    if stats:
        type_counts = {}
        for stat in stats:
            key = (stat.o, stat.d)
            agg = leg_map.setdefault(key, {
                "org": stat.o, "dst": stat.d, "n": 0,
                "per_week": 0.0, "avg_num": 0, "avg_den": 0,
                "types": {}})
            agg["n"] += stat.n_flights
            agg["per_week"] += stat.per_week
            if stat.avg_min is not None:
                agg["avg_num"] += stat.avg_min * stat.n_flights
                agg["avg_den"] += stat.n_flights
            for pair in stat.types or []:
                agg["types"][pair[0]] = agg["types"].get(pair[0], 0) + pair[1]
                type_counts[pair[0]] = 1
            airport_codes.update(key)
        names = {t.designator: t.name for t in session.execute(
            select(RefType).where(RefType.designator.in_(type_counts))).scalars()
        } if type_counts else {}
        legs = []
        for agg in leg_map.values():
            aircraft = sorted(agg["types"].items(), key=lambda x: (-x[1], x[0]))
            legs.append({
                "org": agg["org"], "dst": agg["dst"], "n": agg["n"],
                "per_week": round(agg["per_week"], 2),
                "avg_min": (round(agg["avg_num"] / agg["avg_den"])
                            if agg["avg_den"] else None),
                "aircraft": [{"type": t, "name": names.get(t), "n": n}
                             for t, n in aircraft[:6]],
            })
        source = "flightlog"
    else:
        for chain in session.execute(
                select(RefRoute.chain).where(RefRoute.airline_icao.in_(codes))
        ).scalars() if codes else []:
            for i in range(len(chain) - 1):
                a, b = chain[i], chain[i + 1]
                if not a or not b or a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                leg_map[key] = leg_map.get(key, 0) + 1
                airport_codes.update(key)
        legs = [{"org": a, "dst": b, "n": n, "per_week": None,
                 "aircraft": None} for (a, b), n in leg_map.items()]
        source = "chains"
    airports = _resolve_airports(session, airport_codes)
    legs = [leg for leg in legs
            if leg["org"] in airports and leg["dst"] in airports]
    legs.sort(key=lambda leg: -leg["n"])
    response.headers["Cache-Control"] = CACHE
    return {"slug": row.slug, "source": source,
            "airports": airports, "legs": legs}


def _resolve_airports(session, codes):
    """{code: {lat, lon, name, iso_country, tz}} for the airport codes a
    route touches. Chains carry IATA codes; first airport wins a shared
    code, ICAO ident as a fallback for the few non-IATA codes."""
    airports = {}
    codes = [c for c in codes if c]
    if not codes:
        return airports
    for ap in session.execute(
        select(RefAirport)
        .where(or_(RefAirport.iata.in_(codes), RefAirport.ident.in_(codes)),
               RefAirport.lat.is_not(None))
    ).scalars():
        for code in (ap.iata, ap.ident):
            if code in codes:
                airports.setdefault(code, {
                    "lat": ap.lat, "lon": ap.lon, "name": ap.name,
                    "iso_country": ap.iso_country, "tz": ap.tz})
    return airports


@router.get(
    "/v1/airlines/{icao}/routes", summary="Airline routes",
    description="Observed legs and the airports they touch, undirected "
                "(org/dst in canonical alphabetical order). Prefers "
                "flight-log frequencies; falls back to route chains when "
                "the log has no rows. n is the evidence count, a lower "
                "bound. Rate: 300 per 600 s (bucket `refdata`). Cache: "
                "1 h edge.",
    operation_id="airline_routes",
    responses=spec.ok(spec.EX_AIRLINE_ROUTES, spec.R429, spec.R404),
    openapi_extra=spec.MAP_TIER,
)
def airline_routes(icao: spec.AirlineICAO, request: Request,
                   response: Response, session=Depends(get_session)):
    _throttle(request)
    row = _airline_or_404(session, icao)

    stats = session.execute(
        select(RefLegStat).where(RefLegStat.airline_icao == row.icao)).scalars().all()
    codes, legs = set(), []
    if stats:
        type_codes = set()
        for s in stats:
            for pair in (s.types or []):
                type_codes.add(pair[0])
        names = {t.designator: t.name for t in session.execute(
            select(RefType).where(RefType.designator.in_(list(type_codes))))
            .scalars()} if type_codes else {}
        for s in stats:
            codes.add(s.o)
            codes.add(s.d)
            legs.append({
                "org": s.o, "dst": s.d, "n": s.n_flights,
                "days": s.n_days,
                "per_week": s.per_week, "avg_min": s.avg_min,
                "aircraft": [{"type": p[0], "name": names.get(p[0]),
                              "n": p[1]}
                             for p in (s.types or [])]})
    else:
        agg = {}
        for chain in session.execute(
            select(RefRoute.chain)
            .where(RefRoute.airline_icao == row.icao)).scalars():
            for i in range(len(chain) - 1):
                a, b = chain[i], chain[i + 1]
                if not a or not b or a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                agg[key] = agg.get(key, 0) + 1
        for (a, b), n in agg.items():
            codes.add(a)
            codes.add(b)
            legs.append({"org": a, "dst": b, "n": n, "per_week": None,
                         "aircraft": None})

    airports = _resolve_airports(session, codes)
    out_legs = [leg for leg in legs
                if leg["org"] in airports and leg["dst"] in airports]
    out_legs.sort(key=lambda leg: -leg["n"])
    response.headers["Cache-Control"] = CACHE
    return {"icao": row.icao, "source": "flightlog" if stats else "chains",
            "airports": airports, "legs": out_legs}


@router.get(
    "/v1/airlines/{icao}/leg/{org}/{dst}", summary="Airline leg",
    description="Airframes seen on one undirected airport pair, most "
                "frequent first. Endpoints can be in either order. Rate: "
                "300 per 600 s (bucket `refdata`). Cache: 1 h edge.",
    operation_id="airline_leg",
    responses=spec.ok(spec.EX_LEG, spec.R429, spec.R404),
    openapi_extra=spec.MAP_TIER,
)
def airline_leg(icao: spec.AirlineICAO, org: spec.AirportEnd,
                dst: spec.AirportEnd, request: Request, response: Response,
                session=Depends(get_session)):
    _throttle(request)
    row = _airline_or_404(session, icao)
    org, dst = org.strip().upper(), dst.strip().upper()
    if org > dst:
        org, dst = dst, org
    st = session.get(RefLegStat, (row.icao, org, dst))
    if st is None:
        raise ApiError(404, "not_observed", "unknown leg")
    frames = []
    for entry in (st.airframes or []):
        hexid = entry[0]
        n = entry[1] if len(entry) > 1 else None
        af = session.get(RefAirframe, hexid)
        frames.append({"hex": hexid,
                       "reg": af.registration if af else None,
                       "type": af.type_code if af else None,
                       "flights": n})
    response.headers["Cache-Control"] = CACHE
    return {"icao": row.icao, "org": org, "dst": dst,
            "flights": st.n_flights, "days": st.n_days,
            "airframes": frames}


def _hhmm(minute):
    if minute is None:
        return None
    return "%02d:%02d" % (minute // 60, minute % 60)


@router.get(
    "/v1/airlines/{icao}/schedule/{org}/{dst}",
    summary="Airline schedule",
    description="Inferred timetable for a route, both directions. Times "
                "are HH:MM local at each row's origin. Observation, not a "
                "published schedule. Rate: 300 per 600 s (bucket "
                "`refdata`). Cache: 1 h edge.",
    operation_id="airline_schedule",
    responses=spec.ok(spec.EX_SCHEDULE, spec.R429, spec.R404),
    openapi_extra=spec.MAP_TIER,
)
def airline_schedule(icao: spec.AirlineICAO, org: spec.AirportEnd,
                     dst: spec.AirportEnd, request: Request,
                     response: Response, session=Depends(get_session)):
    """The inferred fixed timetable for a route: every flight number the
    network sees on it, in BOTH directions, with its typical local
    departure time and usual aircraft. Times are the origin airport's
    local time. Flight numbers are shown in IATA form (EK1) when the
    airline's IATA code is known, alongside the raw ICAO callsign."""
    _throttle(request)
    row = _airline_or_404(session, icao)
    org, dst = org.strip().upper(), dst.strip().upper()
    rows = session.execute(
        select(RefSchedule)
        .where(RefSchedule.airline_icao == row.icao,
               RefSchedule.n_flights >=
               request.app.state.settings.schedule_min_flights,
               or_(and_(RefSchedule.org == org, RefSchedule.dst == dst),
                   and_(RefSchedule.org == dst, RefSchedule.dst == org)))
        .order_by(RefSchedule.org, RefSchedule.dep_min)).scalars().all()
    type_names = {
        t.designator: t.name for t in session.execute(
            select(RefType).where(RefType.designator.in_(
                [r.type_code for r in rows if r.type_code]))).scalars()}
    iata = row.iata
    out = []
    for r in rows:
        num = r.callsign[3:].lstrip("0") if len(r.callsign) > 3 else ""
        out.append({
            "callsign": r.callsign,
            "flight": r.flight or ((iata + num) if (iata and num)
                                   else r.callsign),
            "source": r.source,
            "org": r.org, "dst": r.dst,
            "dep": _hhmm(r.dep_min), "dep_min": r.dep_min,
            "arr": _hhmm(r.arr_min), "arr_min": r.arr_min,
            "type": r.type_code, "type_name": type_names.get(r.type_code),
            "n_flights": r.n_flights})
    response.headers["Cache-Control"] = CACHE
    return {"icao": row.icao, "org": org, "dst": dst, "departures": out}


@router.get(
    "/v1/airlines/{icao}/countries", summary="Airline countries",
    description="Countries this airline's observed routes touch. n_routes "
                "is the evidence count, so a consumer can threshold thin "
                "coverage. Rate: 300 per 600 s (bucket `refdata`). Cache: "
                "1 h edge.",
    operation_id="airline_countries",
    responses=spec.ok(spec.EX_COUNTRIES, spec.R429, spec.R404),
    openapi_extra=spec.MAP_TIER,
)
def airline_countries(icao: spec.AirlineICAO, request: Request,
                      response: Response, session=Depends(get_session)):
    _throttle(request)
    row = _airline_or_404(session, icao)
    rows = session.execute(
        select(RefAirlineCountry)
        .where(RefAirlineCountry.airline_icao == row.icao)
        .order_by(RefAirlineCountry.n_routes.desc())).scalars().all()
    response.headers["Cache-Control"] = CACHE
    return {"icao": row.icao,
            "countries": [{"iso_country": c.iso_country,
                           "n_routes": c.n_routes} for c in rows]}


@router.get(
    "/v1/airlines/{icao}/fleet/{designator}",
    summary="Airline fleet type",
    description="Airframes of one type in an airline's observed fleet. "
                "Rate: 300 per 600 s (bucket `refdata`). Cache: 1 h edge.",
    operation_id="airline_fleet_type",
    responses=spec.ok(spec.EX_FLEET_TYPE, spec.R429, spec.R404),
    openapi_extra=spec.MAP_TIER,
)
def airline_fleet_type(icao: spec.AirlineICAO, designator: spec.TypeCode,
                       request: Request, response: Response,
                       session=Depends(get_session)):
    _throttle(request)
    row = _airline_or_404(session, icao)
    designator = designator.strip().upper()
    frames = session.execute(
        select(RefAirframe.hex, RefAirframe.registration)
        .where(or_(RefAirframe.operator_icao == row.icao,
                   RefAirframe.operator_norm == row.name.upper()),
               RefAirframe.type_code == designator)
        .order_by(RefAirframe.registration)
        .limit(300)).all()
    if not frames:
        raise ApiError(404, "not_observed", "no airframes")
    name = session.execute(
        select(RefType.name)
        .where(RefType.designator == designator)).scalar_one_or_none()
    response.headers["Cache-Control"] = CACHE
    return {"icao": row.icao, "type": designator, "type_name": name,
            "airframes": [{"hex": h, "reg": r} for h, r in frames]}


@router.get(
    "/v1/airlines/{icao}/fleet", summary="Airline fleet",
    description="Observed fleet grouped by type. Not a published fleet "
                "list; n_airframes says how much evidence the answer is "
                "built from. Rate: 300 per 600 s (bucket `refdata`). "
                "Cache: 1 h edge.",
    operation_id="airline_fleet",
    responses=spec.ok(spec.EX_FLEET, spec.R429, spec.R404),
    openapi_extra=spec.MAP_TIER,
)
def airline_fleet(icao: spec.AirlineICAO, request: Request, response: Response,
                  session=Depends(get_session)):
    """Observed fleet, grouped by type: airframes we watched flying this
    airline's callsigns (operator_icao, the strong signal), plus any whose
    registry operator matches the name (weak, mostly US)."""
    _throttle(request)
    row = _airline_or_404(session, icao)
    rows = session.execute(
        select(RefAirframe.type_code, func.count())
        .where(or_(RefAirframe.operator_icao == row.icao,
                   RefAirframe.operator_norm == row.name.upper()))
        .group_by(RefAirframe.type_code)
        .order_by(func.count().desc())).all()
    type_names = {
        t.designator: t.name for t in session.execute(
            select(RefType).where(RefType.designator.in_(
                [r[0] for r in rows if r[0]]))).scalars()
    }
    response.headers["Cache-Control"] = CACHE
    return {
        "icao": row.icao,
        "n_airframes": sum(r[1] for r in rows),
        "fleet": [{"type": t, "type_name": type_names.get(t),
                   "count": n} for t, n in rows if t],
        "unknown_type": sum(n for t, n in rows if not t),
    }


@router.get(
    "/v1/types/{designator}", summary="Type",
    description="ICAO type designator to name and category. Rate: 300 per "
                "600 s (bucket `refdata`). Cache: 1 h edge.",
    operation_id="aircraft_type",
    responses=spec.ok(spec.EX_TYPE, spec.R429, spec.R404,
                      schema=spec.SCH_TYPE),
    openapi_extra=spec.STABLE,
)
def aircraft_type(designator: spec.TypeCode, request: Request,
                  response: Response, session=Depends(get_session)):
    _throttle(request)
    row = session.get(RefType, designator.strip().upper())
    if row is None:
        raise ApiError(404, "not_found", "unknown type")
    response.headers["Cache-Control"] = CACHE
    return {"designator": row.designator, "name": row.name,
            "category": row.category}
