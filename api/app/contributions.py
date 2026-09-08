"""Gaps, contributions, the catalog.

The network publishes what observation could not settle (gaps: one end
of a callsign's route seen, the other never), takes answers through one
narrow door, checks each against what was observed, and serves an
answer only after the operator approves it into the catalog. Observation
outranks the catalog whenever both speak; a catalog row that observation
later contradicts is closed, and the question reopens.

    python -m app.contributions list            # pending, with checks
    python -m app.contributions show ID
    python -m app.contributions approve ID [--from YYYY-MM-DD] [--note ..]
    python -m app.contributions reject ID [--note ..]
    python -m app.contributions reconcile       # close rows observation overtook
"""
import argparse
import datetime
import math
import sys

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select

from . import openapi as spec
from . import ratelimit
from .db import get_session, make_sessionmaker
from .errors import ApiError
from .refdata_models import Contribution, RefAirport, RouteCatalog

router = APIRouter()
CACHE = "public, s-maxage=600"
CORRIDOR_DEG = 45          # how far off its last track a claimed end may lie
AGREE_STATUSES = ("pending", "approved")


# ---- lookups ------------------------------------------------------------

def _airport(session, code):
    code = (code or "").strip().upper()
    if not code:
        return None
    return session.execute(
        select(RefAirport).where(or_(RefAirport.iata == code,
                                     RefAirport.ident == code))
        .order_by(RefAirport.iata.is_(None))).scalars().first()


def catalog_current(session, callsign, today=None):
    """The catalog row in force for a callsign, or None."""
    today = today or datetime.date.today()
    return session.execute(
        select(RouteCatalog)
        .where(RouteCatalog.callsign == callsign,
               RouteCatalog.valid_from <= today,
               or_(RouteCatalog.valid_to.is_(None),
                   RouteCatalog.valid_to > today))
        .order_by(RouteCatalog.valid_from.desc())).scalars().first()


def _bearing(lat1, lon1, lat2, lon2):
    la1, la2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(la2)
    y = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) \
        * math.cos(dl)
    return math.degrees(math.atan2(x, y)) % 360


def _angle_between(a, b):
    return abs((a - b + 180) % 360 - 180)


# ---- vetting ------------------------------------------------------------

def vet(session, gap, callsign, origin, dest):
    """Check one route answer against the gap it answers. Returns
    (checks, airport_row): checks is {name: pass | fail | skip}. Nothing
    here decides; the operator does, with these in front of them."""
    checks = {}
    side = gap["side"]
    claimed = dest if side == "dest" else origin
    given_known = origin if side == "dest" else dest
    checks["known_end"] = ("pass" if not given_known
                           or given_known == gap["known"] else "fail")
    checks["not_same"] = "pass" if claimed != gap["known"] else "fail"
    airport = _airport(session, claimed)
    checks["airport"] = ("pass" if airport is not None
                         and airport.role == "commercial" else "fail")
    hint = gap.get("hint")
    if hint:
        checks["observation"] = "pass" if hint == claimed else "fail"
    else:
        checks["observation"] = "skip"
    lat, lon, trk = gap.get("last_lat"), gap.get("last_lon"), gap.get("last_trk")
    if (side == "dest" and airport is not None and airport.lat is not None
            and lat is not None and lon is not None and trk is not None):
        toward = _bearing(lat, lon, airport.lat, airport.lon)
        checks["corridor"] = ("pass" if _angle_between(toward, trk)
                              <= CORRIDOR_DEG else "fail")
    else:
        checks["corridor"] = "skip"
    agree = session.execute(
        select(func.count()).select_from(Contribution)
        .where(Contribution.callsign == callsign,
               Contribution.kind == "route",
               Contribution.status.in_(AGREE_STATUSES),
               Contribution.origin == origin,
               Contribution.dest == dest)).scalar_one()
    checks["agreeing"] = int(agree)
    return checks, airport


def _ok(checks):
    return not any(v == "fail" for v in checks.values())


# ---- routes -------------------------------------------------------------

def _gap_row(callsign, gap):
    return {
        "callsign": callsign,
        "side": gap.get("side"),
        "known": gap.get("known"),
        "hint": gap.get("hint"),
        "n_recent": gap.get("n_recent"),
        "last_seen": gap.get("last_seen"),
        "last_heard": ({"lat": gap["last_lat"], "lon": gap["last_lon"],
                        "track": gap.get("last_trk")}
                       if gap.get("last_lat") is not None else None),
    }


@router.get(
    "/v1/gaps", tags=["Contributions"], summary="Open questions",
    description="Callsigns whose route observation settled at one end "
                "only, most-seen first. Each row says which end is "
                "missing, which is known, and where the aircraft was "
                "last heard. Answer one with POST /v1/contributions. "
                "Rate: 300 per 600 s (bucket `gaps`). Cache: 10 min edge.",
    operation_id="gaps",
    responses=spec.ok(spec.EX_GAPS, spec.R429, spec.R503,
                      schema=spec.SCH_GAPS),
    openapi_extra=spec.STABLE,
)
def gaps(request: Request, response: Response,
         airline: str | None = Query(None, min_length=2, max_length=3,
                                     description="ICAO airline prefix."),
         side: str | None = Query(None, pattern="^(origin|dest)$"),
         limit: int = Query(50, ge=1, le=200),
         offset: int = Query(0, ge=0)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.gaps_rate_limit,
                       settings.rate_window_s, bucket="gaps")
    book = request.app.state.gaps
    if not book.available():
        raise ApiError(503, "artifact_unavailable",
                       "the gaps artifact is not loaded",
                       headers={"Retry-After": "300"})
    rows, total = book.page(offset, limit,
                            airline=(airline or "").upper() or None,
                            side=side)
    response.headers["Cache-Control"] = CACHE
    return {"total": total, "offset": offset,
            "gaps": [_gap_row(cs, g) for cs, g in rows],
            "coverage": "observed"}


@router.get(
    "/v1/gaps/{callsign}", tags=["Contributions"], summary="One question",
    description="The open question for one callsign, with the catalog "
                "answer in force if there is one. 404 when observation "
                "has no question for it. Rate: 300 per 600 s (bucket "
                "`gaps`). Cache: 10 min edge.",
    operation_id="gap",
    responses=spec.ok(spec.EX_GAP, spec.R404, spec.R422, spec.R429,
                      spec.R503, schema=spec.SCH_GAP),
    openapi_extra=spec.STABLE,
)
def gap(callsign: spec.Callsign, request: Request, response: Response,
        session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.gaps_rate_limit,
                       settings.rate_window_s, bucket="gaps")
    callsign = callsign.strip().upper()
    if not (2 <= len(callsign) <= 12) or not callsign.isalnum():
        raise ApiError(422, "invalid_request", "invalid callsign")
    book = request.app.state.gaps
    if not book.available():
        raise ApiError(503, "artifact_unavailable",
                       "the gaps artifact is not loaded",
                       headers={"Retry-After": "300"})
    found = book.get(callsign)
    if found is None:
        raise ApiError(404, "not_found", "no open question")
    out = _gap_row(callsign, found)
    current = catalog_current(session, callsign)
    out["catalog"] = ({"origin": current.origin, "dest": current.dest,
                       "valid_from": str(current.valid_from)}
                      if current else None)
    response.headers["Cache-Control"] = CACHE
    return out


class RouteAnswer(BaseModel):
    kind: str = Field("route", pattern="^route$")
    callsign: str = Field(min_length=2, max_length=12)
    origin: str | None = Field(None, min_length=3, max_length=4)
    dest: str | None = Field(None, min_length=3, max_length=4)
    valid_from: datetime.date | None = None
    note: str | None = Field(None, max_length=280)
    contact: str | None = Field(None, max_length=120)

    @field_validator("callsign", "origin", "dest")
    @classmethod
    def _upper(cls, v):
        if v is None:
            return v
        v = v.strip().upper()
        if not v.isalnum():
            raise ValueError("letters and digits only")
        return v


@router.post(
    "/v1/contributions", tags=["Contributions"], summary="Answer a question",
    status_code=202,
    description="Answer one open question: the callsign and the missing "
                "airport (IATA or ICAO). The answer is checked against "
                "what was observed and held for review; it is served "
                "only once approved into the catalog. 422 when the "
                "callsign has no open question or the airport is not a "
                "commercial field. Rate: 30 per 600 s (bucket "
                "`contribute`).",
    operation_id="contribute",
    responses=spec.ok(spec.EX_CONTRIBUTION, spec.R422, spec.R429, spec.R503,
                      schema=spec.SCH_CONTRIBUTION, status=202),
    openapi_extra=spec.STABLE,
)
def contribute(answer: RouteAnswer, request: Request, response: Response,
               session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.contribute_rate_limit,
                       settings.rate_window_s, bucket="contribute")
    book = request.app.state.gaps
    if not book.available():
        raise ApiError(503, "artifact_unavailable",
                       "the gaps artifact is not loaded",
                       headers={"Retry-After": "300"})
    found = book.get(answer.callsign)
    if found is None:
        raise ApiError(422, "invalid_request",
                       "no open question for this callsign")
    side = found["side"]
    origin = answer.origin or (found["known"] if side == "dest" else None)
    dest = answer.dest or (found["known"] if side == "origin" else None)
    if not origin or not dest:
        raise ApiError(422, "invalid_request",
                       "the missing %s is required" % side)
    checks, airport = vet(session, found, answer.callsign, origin, dest)
    if checks["airport"] == "fail":
        raise ApiError(422, "invalid_request",
                       "%s is not a commercial airport we know"
                       % (dest if side == "dest" else origin))
    code = airport.iata or airport.ident        # the missing end, canonical
    if side == "dest":
        dest = code
    else:
        origin = code
    row = Contribution(
        kind="route", callsign=answer.callsign, origin=origin, dest=dest,
        valid_from=answer.valid_from, note=answer.note,
        contact=answer.contact, status="pending",
        checks={"ok": _ok(checks), "checks": checks},
        submitted_at=datetime.datetime.now(datetime.timezone.utc))
    session.add(row)
    session.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"id": row.id, "status": row.status, "callsign": row.callsign,
            "origin": row.origin, "dest": row.dest, "checks": checks}


# ---- review -------------------------------------------------------------

def approve(session, contribution_id, valid_from=None, note=None):
    """Copy an answer into the catalog, closing whatever row it replaces."""
    row = session.get(Contribution, contribution_id)
    if row is None or row.status != "pending":
        raise ValueError("no pending contribution %s" % contribution_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    start = valid_from or row.valid_from or now.date()
    for old in session.execute(
            select(RouteCatalog).where(RouteCatalog.callsign == row.callsign,
                                       RouteCatalog.valid_to.is_(None))
    ).scalars():
        old.valid_to = start
        old.closed_reason = "superseded"
    session.add(RouteCatalog(
        callsign=row.callsign, origin=row.origin, dest=row.dest,
        valid_from=start, source="community", contribution_id=row.id,
        approved_at=now))
    row.status, row.reviewed_at, row.review_note = "approved", now, note
    session.commit()
    return row


def reject(session, contribution_id, note=None):
    row = session.get(Contribution, contribution_id)
    if row is None or row.status != "pending":
        raise ValueError("no pending contribution %s" % contribution_id)
    row.status = "rejected"
    row.reviewed_at = datetime.datetime.now(datetime.timezone.utc)
    row.review_note = note
    session.commit()
    return row


def reconcile(session, routes_book, today=None):
    """Close catalog rows observation has overtaken: the same route now
    observed end to end (the row is no longer needed) or a different one
    (the row was wrong, or the schedule moved). Returns (closed, reason)
    pairs."""
    today = today or datetime.date.today()
    closed = []
    for row in session.execute(
            select(RouteCatalog).where(RouteCatalog.valid_to.is_(None))
    ).scalars():
        observed = routes_book.get(row.callsign)
        if not observed:
            continue
        same = observed[0] == row.origin and observed[-1] == row.dest
        row.valid_to = today
        row.closed_reason = "observed" if same else "contradicted"
        closed.append((row.callsign, row.closed_reason))
    session.commit()
    return closed


def _print_row(row):
    checks = (row.checks or {}).get("checks", {})
    print("#%-5d %-8s %s -> %s  %s  %s" % (
        row.id, row.callsign, row.origin or "?", row.dest or "?",
        row.status, row.submitted_at.strftime("%Y-%m-%d")))
    print("       " + "  ".join("%s:%s" % kv for kv in checks.items()))
    if row.note:
        print("       note: %s" % row.note)
    if row.contact:
        print("       from: %s" % row.contact)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("show"); p.add_argument("id", type=int)
    p = sub.add_parser("approve"); p.add_argument("id", type=int)
    p.add_argument("--from", dest="valid_from", type=datetime.date.fromisoformat)
    p.add_argument("--note")
    p = sub.add_parser("reject"); p.add_argument("id", type=int)
    p.add_argument("--note")
    sub.add_parser("reconcile")
    args = ap.parse_args(argv)

    from .routes_db import RouteBook
    from .settings import Settings
    settings = Settings()
    session = make_sessionmaker(settings.database_url)()
    try:
        if args.cmd == "list":
            rows = session.execute(
                select(Contribution).where(Contribution.status == "pending")
                .order_by(Contribution.submitted_at)).scalars().all()
            for row in rows:
                _print_row(row)
            print("%d pending" % len(rows))
        elif args.cmd == "show":
            row = session.get(Contribution, args.id)
            if row is None:
                sys.exit("no contribution %d" % args.id)
            _print_row(row)
        elif args.cmd == "approve":
            row = approve(session, args.id, args.valid_from, args.note)
            print("approved #%d %s %s -> %s" % (row.id, row.callsign,
                                               row.origin, row.dest))
        elif args.cmd == "reject":
            row = reject(session, args.id, args.note)
            print("rejected #%d %s" % (row.id, row.callsign))
        elif args.cmd == "reconcile":
            closed = reconcile(session, RouteBook(settings.routes_path))
            for callsign, reason in closed:
                print("closed %s: %s" % (callsign, reason))
            print("%d closed" % len(closed))
    finally:
        session.close()


if __name__ == "__main__":
    main()
