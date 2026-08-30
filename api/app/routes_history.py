"""History routes: one aircraft, one flight number, one airport.

Each resource merges every layer the instance holds about its subject —
registry identity, the observed flight log, the derived route table, the
inferred timetable — so a consumer needs one request, not a join across
endpoints. All observation: gaps mean the network's sources did not hear
it, and every body says so (`coverage: "observed"`).

Artifact posture: a section fed by an artifact that is NOT loaded is null
(unknown), never an empty list (observed nothing). When every source a
resource depends on is dark, the route serves 503 `artifact_unavailable`
— a fact about our ops — instead of a 404 that would claim a fact about
the world.
"""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from . import openapi as spec
from . import ratelimit
from .db import get_session
from .errors import ApiError
from .refdata_models import RefAirframe, RefAirline, RefAirport, RefSchedule, \
    RefType
from .routes_refdata import _hhmm, _memberships_by_airline, _serialize_airline

router = APIRouter(tags=["History"])

CACHE = "public, s-maxage=3600"
RETRY_DARK = {"Retry-After": "300"}      # artifact reload cadence


def _dark() -> ApiError:
    return ApiError(503, "artifact_unavailable",
                    "history artifacts are not loaded", headers=RETRY_DARK)


@router.get(
    "/v1/airframes/{hex}", tags=["History"], summary="Airframe",
    description="One aircraft: registry identity and the observed flight "
                "log, newest first. legs is null when the log artifact is "
                "not loaded, empty when the aircraft was not seen in the "
                "window. Observation, not a registry of record. 404 if "
                "nothing is known. Rate: 300 per 600 s (bucket "
                "`airframe`). Cache: 1 h edge.",
    operation_id="airframe",
    responses=spec.ok(spec.EX_AIRFRAME, spec.R429, spec.R404, spec.R422,
                      spec.R503, schema=spec.SCH_AIRFRAME),
    openapi_extra=spec.STABLE,
)
def airframe(hex: spec.Hex, request: Request, response: Response,
             session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.airframe_rate_limit,
                       settings.rate_window_s, bucket="airframe")
    hex_id = hex.strip().lower()
    if len(hex_id) != 6 or any(c not in "0123456789abcdef" for c in hex_id):
        raise ApiError(422, "invalid_request", "invalid hex")

    legs_book = request.app.state.legs
    log = legs_book.get(hex_id) if legs_book.available() else None
    frame = session.get(RefAirframe, hex_id)
    if frame is None and log is None:
        if not legs_book.available():
            # the registry is silent and the log is dark: unknowable
            raise _dark()
        raise ApiError(404, "not_found", "unknown airframe")

    out = {
        "hex": hex_id,
        "reg": frame.registration if frame else None,
        "type": frame.type_code if frame else None,
        "type_name": None, "category": None,
        "operator": frame.operator_name if frame else None,
        "operator_icao": frame.operator_icao if frame else None,
        "year": frame.year if frame else None,
        "source": frame.source if frame else None,
        "airline": None,
        "legs": None, "window_days": None,
        "coverage": "observed",
    }
    if log is not None:
        out["legs"] = log["legs"]
        out["window_days"] = legs_book.window_days()
        # the log can carry identity the registry lacks
        out["reg"] = out["reg"] or log.get("reg")
        out["type"] = out["type"] or log.get("type")
    elif legs_book.available():
        out["legs"] = []
        out["window_days"] = legs_book.window_days()
    if out["operator_icao"]:
        airline = session.get(RefAirline, out["operator_icao"])
        if airline is not None:
            out["operator"] = out["operator"] or airline.name
            memberships = _memberships_by_airline(session, [airline.icao])
            out["airline"] = _serialize_airline(
                airline, memberships.get(airline.icao))
    if out["type"]:
        ref_type = session.get(RefType, out["type"])
        if ref_type is not None:
            out["type_name"] = ref_type.name
            out["category"] = ref_type.category
    response.headers["Cache-Control"] = CACHE
    return out


@router.get(
    "/v1/flights/{callsign}", tags=["History"], summary="Flight",
    description="One flight number: derived route, observed legs with "
                "typical local times, the airframes that fly it, recent "
                "operations. Sections fed by an unloaded artifact are "
                "null. Observation, not a published schedule. 404 if "
                "never observed. Rate: 120 per 600 s (bucket `flight`). "
                "Cache: 1 h edge.",
    operation_id="flight",
    responses=spec.ok(spec.EX_FLIGHT, spec.R429, spec.R404, spec.R422,
                      spec.R503, schema=spec.SCH_FLIGHT),
    openapi_extra=spec.STABLE,
)
def flight(callsign: spec.Callsign, request: Request, response: Response,
           session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.flight_rate_limit,
                       settings.rate_window_s, bucket="flight")
    callsign = callsign.strip().upper()
    if not (2 <= len(callsign) <= 12) or not callsign.isalnum():
        raise ApiError(422, "invalid_request", "invalid flight")

    routes_book = request.app.state.routes
    legs_book = request.app.state.legs
    routes_up = routes_book.available()
    legs_up = legs_book.available()
    if not routes_up and not legs_up:
        raise _dark()
    route = routes_book.get(callsign) if routes_up else None
    log = legs_book.flight(callsign) if legs_up else None
    if route is None and log is None:
        raise ApiError(404, "not_observed", "no observations")

    out = {
        "callsign": callsign,
        "route": route,
        "legs": None, "aircraft": None, "recent": None,
        "window_days": legs_book.window_days() if legs_up else None,
        "coverage": "observed",
    }
    if log is not None:
        out.update(legs=log["legs"], aircraft=log["aircraft"],
                   recent=log["recent"])
    elif legs_up:
        out.update(legs=[], aircraft=[], recent=[])
    if out["legs"]:
        sched = {}
        for r in session.execute(
                select(RefSchedule).where(
                    RefSchedule.callsign == callsign)).scalars():
            sched[(r.org, r.dst)] = r
        for leg in out["legs"]:
            row = sched.get((leg["org"], leg["dst"]))
            leg["dep"] = _hhmm(row.dep_min) if row else None
            leg["arr"] = _hhmm(row.arr_min) if row else None
            leg["type"] = row.type_code if row else None
    response.headers["Cache-Control"] = CACHE
    return out


@router.get(
    "/v1/airports/{code}", tags=["History"], summary="Airport",
    description="One airport, IATA or ICAO code: registry identity, "
                "observed totals and busiest routes, and the inferred "
                "departures board in the airport's LOCAL time. Circuits "
                "excluded. observed is null when the log artifact is not "
                "loaded. 404 if unknown. Rate: 120 per 600 s (bucket "
                "`airport`). Cache: 1 h edge.",
    operation_id="airport",
    responses=spec.ok(spec.EX_AIRPORT, spec.R429, spec.R404, spec.R422,
                      spec.R503, schema=spec.SCH_AIRPORT),
    openapi_extra=spec.STABLE,
)
def airport(code: spec.AirportCode, request: Request, response: Response,
            session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.airport_rate_limit,
                       settings.rate_window_s, bucket="airport")
    code = code.strip().upper()
    if not (3 <= len(code) <= 4) or not code.isalnum():
        raise ApiError(422, "invalid_request", "invalid airport code")

    # Registry resolution: ICAO ident first, IATA second. The flight log
    # and schedule are keyed by IATA, so either spelling lands on the
    # same airport.
    reg = session.get(RefAirport, code)
    if reg is None and len(code) == 3:
        reg = session.execute(
            select(RefAirport).where(RefAirport.iata == code)
            .order_by(RefAirport.ident)).scalars().first()
    iata = (reg.iata if reg else None) or (code if len(code) == 3 else None)

    legs_book = request.app.state.legs
    legs_up = legs_book.available()
    observed = legs_book.airport(iata) if legs_up and iata else None
    if observed is not None:
        observed.pop("code", None)
    if reg is None and observed is None:
        if not legs_up and len(code) == 3:
            # not in the registry, and the one source that could still
            # know the code is dark
            raise _dark()
        raise ApiError(404, "not_found", "unknown airport")

    board = []
    airlines = {}
    if iata:
        for r in session.execute(
                select(RefSchedule)
                .where(RefSchedule.org == iata,
                       RefSchedule.n_flights >= settings.schedule_min_flights)
                .order_by(RefSchedule.n_flights.desc())
                .limit(80)).scalars():
            board.append({"flight": r.flight or r.callsign, "dst": r.dst,
                          "dep": _hhmm(r.dep_min), "arr": _hhmm(r.arr_min),
                          "type": r.type_code, "flights": r.n_flights,
                          "source": r.source})
            if r.airline_icao:
                airlines[r.airline_icao] = (airlines.get(r.airline_icao, 0)
                                            + r.n_flights)
    board.sort(key=lambda b: (b["dep"] is None, b["dep"] or ""))
    response.headers["Cache-Control"] = CACHE
    return {
        "iata": iata,
        "ident": reg.ident if reg else None,
        "name": reg.name if reg else None,
        "kind": reg.kind if reg else None,
        "lat": reg.lat if reg else None,
        "lon": reg.lon if reg else None,
        "iso_country": reg.iso_country if reg else None,
        "municipality": reg.municipality if reg else None,
        "tz": reg.tz if reg else None,
        "observed": observed,
        "board": board,
        "airlines": [{"icao": k, "flights": v} for k, v in
                     sorted(airlines.items(), key=lambda kv: -kv[1])[:20]],
        "times": "local",
        "window_days": legs_book.window_days() if legs_up else None,
        "coverage": "observed",
    }
