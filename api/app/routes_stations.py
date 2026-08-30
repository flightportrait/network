"""Station routes: the public roster and the feeder's self-service view.

The self view's credential is knowledge of the full station UUID — the
same secret the feeder already holds in their readsb config. Malformed,
unknown, and wrong UUIDs are indistinguishable (uniform 404): the roster
deliberately supports no enumeration path back to identities."""
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from . import openapi as spec
from . import ratelimit
from . import stations as stations_logic
from .errors import ApiError
from .db import get_session
from .models import Station

router = APIRouter(tags=["Stations"])


@router.get(
    "/v1/stations", summary="Roster",
    description="Public feeder list. Locations rounded to about 11 km. "
                "IPs are not stored. Rate: 120 per 600 s (bucket "
                "`stations`). Cache: 30 s edge.",
    operation_id="stations",
    responses=spec.ok(spec.EX_STATIONS, spec.R429,
                      schema=spec.SCH_STATIONS),
    openapi_extra=spec.STABLE,
)
def roster(request: Request, response: Response,
           session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.stations_rate_limit,
                       settings.rate_window_s, bucket="stations")
    rows = session.execute(
        select(Station).order_by(Station.first_seen)
    ).scalars().all()
    response.headers["Cache-Control"] = "public, s-maxage=30"
    return {
        "stations": [
            stations_logic.serialize_public(s, settings.offline_after_s)
            for s in rows
        ],
    }


@router.get(
    "/v1/stations/{station_uuid}", summary="Station",
    description="Status for one feeder. The full UUID is the key; the "
                "server stores a hash. Unknown and malformed UUIDs both "
                "return 404. The UUID travels in the URL: treat the link "
                "as the secret it carries. Rate: 60 per 600 s (bucket "
                "`station_detail`). Never cached.",
    operation_id="station",
    responses=spec.ok(spec.EX_STATION, spec.R429, spec.R404,
                      schema=spec.SCH_STATION),
    openapi_extra=spec.STABLE,
)
async def self_view(station_uuid: spec.StationUUID, request: Request,
                    response: Response, session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.station_detail_rate_limit,
                       settings.rate_window_s, bucket="station_detail")
    response.headers["Cache-Control"] = "no-store"

    station = stations_logic.lookup_by_uuid(session, station_uuid)
    if station is None:
        raise ApiError(404, "not_found", "unknown station")

    live = request.app.state.presence.get(station.half_id)
    aircraft_seen = None
    if live is not None:
        try:
            aircraft_seen = await request.app.state.readsb.count_for_station(
                station.half_id)
        except Exception:      # noqa: BLE001 — stats degrade, never 500
            aircraft_seen = None
    return stations_logic.serialize_self(
        station, live, aircraft_seen, settings.offline_after_s)
