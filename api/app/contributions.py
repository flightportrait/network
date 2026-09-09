"""Gaps, claims, the catalog.

The network publishes what observation could not settle: callsigns
whose route is known at one end only. Answers arrive at the edge door
(contribute/), never here; this service pulls them, files each as a
claim with the people behind it, checks the claim against what was
observed, and serves it only once approved into the catalog. A claim
the evidence corroborates approves itself; one the evidence contradicts
rejects itself; the rest wait for the operator. Observation outranks
the catalog whenever both speak.

    python -m app.contributions pull            # file new submissions
    python -m app.contributions list            # pending, with checks
    python -m app.contributions show ID
    python -m app.contributions approve ID [--from YYYY-MM-DD] [--note ..]
    python -m app.contributions reject ID [--note ..]
    python -m app.contributions withdraw ID [--note ..]   # undo an approval
    python -m app.contributions reconcile       # close rows observation overtook
    python -m app.contributions propose         # file what the evidence points at
"""
import argparse
import datetime
import json
import math
import re
import sys
import urllib.request

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, or_, select

from . import openapi as spec
from . import ratelimit
from .db import get_session, make_sessionmaker
from .errors import ApiError
from .refdata_models import Claim, Endorsement, PullState, RefAirport, \
    RouteCatalog

router = APIRouter()
CACHE = "public, s-maxage=600"
CORRIDOR_DEG = 45
ROTATION_TOLERANCE = 0.25
ROTATION_FLOOR_KM = 400
MIN_ROTATIONS = 2
PROPOSE_ROTATIONS = 3
SUGGESTIONS = 3
MAX_CLAIMS_PER_QUESTION = 3
PURGE_AFTER_DAYS = 90
PULL_PAGE = 500
FLIGHT_NUMBER = re.compile(r"^([A-Z]{3})0*(\d+)[A-Z]{0,2}$")

# Still-air range, km, by ICAO type designator: a claim beyond it is wrong.
RANGE_KM = {
    "A318": 5700, "A319": 6900, "A320": 6100, "A321": 5900, "A19N": 6900,
    "A20N": 6300, "A21N": 7400, "A306": 7500, "A310": 9600, "A332": 13400,
    "A333": 11700, "A338": 15000, "A339": 13300, "A342": 13800, "A343": 13500,
    "A345": 16000, "A346": 14400, "A359": 15000, "A35K": 16000, "A388": 15000,
    "B712": 3800, "B733": 4400, "B734": 4200, "B735": 4400, "B736": 5600,
    "B737": 6300, "B738": 5700, "B739": 5900, "B37M": 7000, "B38M": 6500,
    "B39M": 6500, "B3XM": 6100, "B744": 13400, "B748": 14300, "B752": 7200,
    "B753": 6400, "B762": 7300, "B763": 11000, "B764": 10400, "B772": 9700,
    "B773": 11000, "B77L": 15800, "B77W": 14000, "B788": 13600, "B789": 14000,
    "B78X": 11900, "MD11": 12000, "MD82": 3800, "MD83": 4600, "MD88": 4100,
    "E170": 3900, "E175": 4000, "E75L": 4000, "E190": 4500, "E195": 4200,
    "E290": 5300, "E295": 4900, "CRJ2": 3000, "CRJ7": 3600, "CRJ9": 2900,
    "CRJX": 3000, "AT72": 1500, "AT75": 1500, "AT76": 1500, "DH8D": 2000,
    "DH8C": 1700, "DH8B": 1700, "DH8A": 1700, "BCS1": 6400, "BCS3": 6300,
    "SU95": 4600, "C919": 5500,
}


# ---- lookups ------------------------------------------------------------

def _airport(session, code):
    code = (code or "").strip().upper()
    if not code:
        return None
    return session.execute(
        select(RefAirport).where(or_(RefAirport.iata == code,
                                     RefAirport.ident == code))
        .order_by(RefAirport.iata.is_(None))).scalars().first()


def catalog_route(row):
    return [row.origin, *(row.via or []), row.dest]


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


def _distance_km(lat1, lon1, lat2, lon2):
    la1, la2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) \
        * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(h)))


def _mirror_numbers(callsign):
    """The flight numbers one apart: an outbound's return, usually."""
    m = FLIGHT_NUMBER.match(callsign)
    if not m:
        return []
    prefix, number = m.group(1), int(m.group(2))
    return [prefix + str(n) for n in (number - 1, number + 1) if n > 0]


# ---- candidates ---------------------------------------------------------

class AirportIndex:
    """Commercial airports with coordinates, loaded once per process
    from the reference table; the candidate search walks them all."""

    def __init__(self):
        self.rows = None

    def load(self, session):
        if self.rows is None:
            self.rows = [(a.iata or a.ident, a.lat, a.lon) for a in
                         session.execute(select(RefAirport).where(
                             RefAirport.role == "commercial",
                             RefAirport.lat.is_not(None))).scalars()]
        return self.rows


def candidates(session, airports, network, gap, callsign, limit=SUGGESTIONS):
    """Airports the evidence allows for the missing end, best first:
    inside the ring the rotation implies, within the type's range,
    along the last heard track, never the known end or a stop already
    in the chain. Ranked by how much the airline flies there; airports
    the airline is never seen at come last. [] when the rotation is
    still unknown."""
    est = gap.get("est_km")
    if not est or (gap.get("n_rot") or 0) < MIN_ROTATIONS:
        return []
    known = _airport(session, gap["known"])
    if known is None or known.lat is None:
        return []
    tolerance = max(ROTATION_FLOOR_KM, ROTATION_TOLERANCE * est)
    reach = RANGE_KM.get(gap.get("type") or "")
    lat, lon, trk = gap.get("last_lat"), gap.get("last_lon"), gap.get("last_trk")
    heading = (gap["side"] == "dest" and lat is not None and lon is not None
               and trk is not None)
    served = network.get(callsign[:3], {})
    exclude = {gap["known"], *(gap.get("chain") or [])}
    out = []
    for code, alat, alon in airports.load(session):
        if code in exclude:
            continue
        if abs(alat - known.lat) * 111 > est + tolerance:
            continue
        d = _distance_km(known.lat, known.lon, alat, alon)
        if abs(d - est) > tolerance or (reach and d > reach):
            continue
        if heading and _angle_between(_bearing(lat, lon, alat, alon), trk) \
                > CORRIDOR_DEG:
            continue
        out.append((-(served.get(code, 0)), abs(d - est), code))
    out.sort()
    return [code for _, _, code in out[:limit]]


def unique_candidate(session, airports, network, gap, callsign):
    """The one airport the airline is known to fly to that fits every
    filter, or None when there is none or more than one."""
    fitting = candidates(session, airports, network, gap, callsign, limit=50)
    served = network.get(callsign[:3], {})
    in_network = [c for c in fitting if served.get(c)]
    return in_network[0] if len(in_network) == 1 else None


# ---- checks -------------------------------------------------------------

def check_claim(session, book, claim, airports=None, network=None,
                legs=None):
    """Every test the evidence allows, as {name: pass | fail | skip} plus
    the counts of people behind the claim. verdict() reads the set."""
    gap = book.get(claim.callsign)
    if gap is None:
        return {"asked": "fail"}
    checks = {"asked": "pass"}
    side = gap["side"]
    claimed = claim.dest if side == "dest" else claim.origin
    given_known = claim.origin if side == "dest" else claim.dest
    checks["known_end"] = "pass" if given_known == gap["known"] else "fail"
    checks["not_same"] = "pass" if claimed != gap["known"] else "fail"
    airport = _airport(session, claimed)
    known = _airport(session, gap["known"])
    checks["airport"] = ("pass" if airport is not None
                         and airport.role == "commercial" else "fail")
    hint = gap.get("hint")
    checks["observation"] = ("pass" if hint == claimed else "fail") \
        if hint else "skip"

    have_geo = (airport is not None and known is not None
                and airport.lat is not None and known.lat is not None)
    distance = (_distance_km(known.lat, known.lon, airport.lat, airport.lon)
                if have_geo else None)

    lat, lon, trk = gap.get("last_lat"), gap.get("last_lon"), gap.get("last_trk")
    if (side == "dest" and airport is not None and airport.lat is not None
            and lat is not None and lon is not None and trk is not None):
        toward = _bearing(lat, lon, airport.lat, airport.lon)
        checks["corridor"] = ("pass" if _angle_between(toward, trk)
                              <= CORRIDOR_DEG else "fail")
    else:
        checks["corridor"] = "skip"

    est = gap.get("est_km")
    if distance is not None and est and (gap.get("n_rot") or 0) >= MIN_ROTATIONS:
        tolerance = max(ROTATION_FLOOR_KM, ROTATION_TOLERANCE * est)
        checks["rotation"] = ("pass" if abs(distance - est) <= tolerance
                              else "fail")
    else:
        checks["rotation"] = "skip"

    reach = RANGE_KM.get(gap.get("type") or "")
    if distance is not None and reach:
        checks["type"] = "pass" if distance <= reach else "fail"
    else:
        checks["type"] = "skip"

    checks["mirror"] = "skip"
    for other in _mirror_numbers(claim.callsign):
        mirror = book.get(other)
        if not mirror or mirror["side"] == side or mirror["known"] != gap["known"]:
            continue
        current = catalog_current(session, other)
        answer = ((current.origin if side == "dest" else current.dest)
                  if current else mirror.get("hint"))
        if answer:
            checks["mirror"] = "pass" if answer == claimed else "fail"
            break

    checks["log"] = "skip"
    log = legs.flight(claim.callsign) if legs is not None and legs.available() \
        else None
    if log:
        pair = (gap["known"], claimed) if side == "dest" else (claimed, gap["known"])
        cutoff = (datetime.date.today()
                  - datetime.timedelta(days=LOG_RECENT_DAYS)).isoformat()
        seen = sum(l["flights"] for l in log["legs"]
                   if (l["org"], l["dst"]) == pair and l["last"] >= cutoff)
        elsewhere = sum(l["flights"] for l in log["legs"]
                        if (l["org"], l["dst"]) != pair
                        and gap["known"] in (l["org"], l["dst"])
                        and l["last"] >= cutoff)
        if seen >= LOG_MIN_FLIGHTS:
            checks["log"] = "pass"
        elif elsewhere >= LOG_ELSEWHERE_FLIGHTS:
            checks["log"] = "fail"

    checks["unique"] = "skip"
    if airports is not None and network is not None:
        sole = unique_candidate(session, airports, network, gap, claim.callsign)
        if sole is not None:
            checks["unique"] = "pass" if sole == claimed else "fail"
    served = (network or {}).get(claim.callsign[:3], {})
    checks["network"] = ("pass" if served.get(claimed) else "fail") \
        if served else "skip"

    checks["keyed"] = int(session.execute(
        select(func.count(func.distinct(Endorsement.key_name)))
        .where(Endorsement.claim_id == claim.id,
               Endorsement.key_name.is_not(None))).scalar_one())
    checks["named"] = int(session.execute(
        select(func.count(func.distinct(Endorsement.handle)))
        .where(Endorsement.claim_id == claim.id,
               Endorsement.handle.is_not(None))).scalar_one())
    checks["anonymous"] = int(claim.anonymous_count)
    return checks


# Impossible on its face: no such question, wrong known end, an airport
# that is not one, a distance the aircraft cannot fly.
HARD = ("asked", "known_end", "not_same", "airport", "type")
# Evidence that can agree or disagree; a disagreement is a reason for a
# person to look, never a rejection on its own.
SOFT = ("corridor", "rotation", "mirror", "observation", "unique", "log")
LOG_MIN_FLIGHTS = 2
LOG_ELSEWHERE_FLIGHTS = 10
LOG_RECENT_DAYS = 90


def weighed(checks):
    """The checks as the verdict reads them. The log saw the pair flown;
    the rotation is an inference that assumes the airframe turns
    straight back, which spoke-to-hub flights rarely do. Observation
    outranks the inference."""
    checks = dict(checks or {})
    if checks.get("log") == "pass" and checks.get("rotation") == "fail":
        checks["rotation"] = "skip"
    return checks


def disagrees(checks):
    checks = weighed(checks)
    return any(checks.get(name) == "fail" for name in HARD + SOFT)


def verdict(checks):
    if any(checks.get(name) == "fail" for name in HARD):
        return "contradicted"
    checks = weighed(checks)
    if any(checks.get(name) == "fail" for name in SOFT):
        return "unverified"
    signals = sum(1 for name in SOFT if checks.get(name) == "pass")
    if checks.get("keyed", 0) >= 2:
        signals += 1
    return "corroborated" if signals >= 2 else "unverified"


# ---- filing -------------------------------------------------------------

def file_submission(session, book, sub, now=None):
    """One edge submission into a claim and, when it says something,
    an endorsement. Returns the claim, or None when dropped."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    callsign = (sub.get("callsign") or "").strip().upper()
    gap = book.get(callsign)
    if gap is None:
        return None
    side = gap["side"]
    missing = _airport(session, sub.get("dest" if side == "dest" else "origin"))
    if missing is None:
        return None
    code = missing.iata or missing.ident
    origin, dest = (gap["known"], code) if side == "dest" else (code, gap["known"])
    claim = session.execute(
        select(Claim).where(Claim.callsign == callsign, Claim.origin == origin,
                            Claim.dest == dest)).scalars().first()
    if claim is None:
        open_claims = session.execute(
            select(func.count()).select_from(Claim)
            .where(Claim.callsign == callsign,
                   Claim.status != "rejected")).scalar_one()
        if open_claims >= MAX_CLAIMS_PER_QUESTION:
            for other in session.execute(
                    select(Claim).where(Claim.callsign == callsign,
                                        Claim.status == "pending")).scalars():
                other.verdict = "contested"
            return None
        claim = Claim(callsign=callsign, origin=origin, dest=dest,
                      status="pending", anonymous_count=0,
                      first_at=now, last_at=now)
        session.add(claim)
        session.flush()
    claim.last_at = now
    handle, note, key = sub.get("handle"), sub.get("note"), sub.get("key_name")
    valid_from = sub.get("valid_from")
    if isinstance(valid_from, str):
        try:
            valid_from = datetime.date.fromisoformat(valid_from)
        except ValueError:
            valid_from = None
    if handle or note or key:
        session.add(Endorsement(
            claim_id=claim.id, handle=handle, note=note, key_name=key,
            valid_from=valid_from, received_at=now,
            source_id=sub.get("id")))
    else:
        claim.anonymous_count += 1
    session.flush()
    return claim


def evaluate(session, book, claim, now=None, airports=None, network=None,
             legs=None):
    """Run the checks, record the verdict, act when the evidence is
    decisive. Pending claims are evaluated on every pull, since the
    artifact behind the checks refreshes nightly."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    claim.checks = check_claim(session, book, claim, airports, network, legs)
    if claim.status != "pending" or claim.verdict == "contested":
        return claim.status
    claim.verdict = verdict(claim.checks)
    if claim.verdict == "corroborated":
        _approve(session, book, claim, now, by="verdict")
    elif claim.verdict == "contradicted":
        claim.status, claim.reviewed_at = "rejected", now
        claim.reviewed_by = "verdict"
    return claim.status


def _approve(session, book, claim, now, by, valid_from=None, note=None):
    gap = book.get(claim.callsign) or {}
    chain = gap.get("chain") or []
    origin, via, dest = claim.origin, [], claim.dest
    if chain:
        if gap.get("side") == "origin":
            via, dest = chain[:-1], chain[-1]
        else:
            origin, via = chain[0], chain[1:]
    start = valid_from
    if start is None:
        starts = [e.valid_from for e in session.execute(
            select(Endorsement).where(Endorsement.claim_id == claim.id)
        ).scalars() if e.valid_from]
        start = min(starts) if starts else now.date()
    for old in session.execute(
            select(RouteCatalog).where(RouteCatalog.callsign == claim.callsign,
                                       RouteCatalog.valid_to.is_(None))
    ).scalars():
        old.valid_to = start
        old.closed_reason = "superseded"
    session.add(RouteCatalog(
        callsign=claim.callsign, origin=origin, dest=dest, via=via or None,
        valid_from=start, source="community", claim_id=claim.id,
        approved_at=now))
    claim.status, claim.reviewed_at, claim.reviewed_by = "approved", now, by
    claim.review_note = note


# ---- pull ---------------------------------------------------------------

def fetch_submissions(url, token, after, limit=PULL_PAGE):
    req = urllib.request.Request(
        "%s/pull?after=%d&limit=%d" % (url.rstrip("/"), after, limit),
        headers={"Authorization": "Bearer " + token,
                 "User-Agent": "flightportrait-network-api"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("submissions", [])


def pull(session, book, fetch, now=None, routes=None, legs=None):
    """File everything new at the edge, evaluate every pending claim,
    purge what is old and rejected. Returns the summary counts."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    airports = AirportIndex()
    network = routes.by_airline() if routes is not None else {}
    state = session.get(PullState, 1)
    if state is None:
        state = PullState(id=1, cursor=0)
        session.add(state)
        session.flush()
    counts = {"received": 0, "filed": 0, "dropped": 0}
    while True:
        batch = fetch(state.cursor)
        for sub in batch:
            counts["received"] += 1
            if file_submission(session, book, sub, now) is None:
                counts["dropped"] += 1
            else:
                counts["filed"] += 1
            state.cursor = max(state.cursor, int(sub["id"]))
        session.commit()
        if len(batch) < PULL_PAGE:
            break
    state.pulled_at = now
    counts.update({"approved": 0, "rejected": 0})
    for claim in session.execute(
            select(Claim).where(Claim.status == "pending")).scalars().all():
        status = evaluate(session, book, claim, now, airports, network, legs)
        if status in ("approved", "rejected"):
            counts[status] += 1
    cutoff = now - datetime.timedelta(days=PURGE_AFTER_DAYS)
    stale = session.execute(
        select(Claim).where(Claim.status == "rejected",
                            Claim.reviewed_at < cutoff)).scalars().all()
    for claim in stale:
        for e in session.execute(select(Endorsement).where(
                Endorsement.claim_id == claim.id)).scalars():
            session.delete(e)
        session.delete(claim)
    counts["purged"] = len(stale)
    counts["pending"] = session.execute(
        select(func.count()).select_from(Claim)
        .where(Claim.status == "pending")).scalar_one()
    session.commit()
    return counts


def propose(session, book, routes, now=None, legs=None):
    """Where the evidence leaves exactly one airport the airline flies
    to, file that as a claim and judge it like any other. Returns
    (filed, approved). Questions with an open or approved claim, or too
    few rotations, are left alone."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    airports = AirportIndex()
    network = routes.by_airline()
    taken = {cs for (cs,) in session.execute(
        select(Claim.callsign).where(Claim.status != "rejected"))}
    filed = approved = 0
    for callsign, gap in book.page(0, book.count())[0]:
        if callsign in taken or (gap.get("n_rot") or 0) < PROPOSE_ROTATIONS:
            continue
        code = unique_candidate(session, airports, network, gap, callsign)
        if code is None:
            continue
        sub = {"callsign": callsign, "key_name": "evidence",
               "note": "the one airport the airline flies to at this "
                       "distance and heading"}
        sub["dest" if gap["side"] == "dest" else "origin"] = code
        claim = file_submission(session, book, sub, now)
        if claim is None:
            continue
        filed += 1
        if evaluate(session, book, claim, now, airports, network,
                    legs) == "approved":
            approved += 1
        if filed % 200 == 0:
            session.commit()
    session.commit()
    return filed, approved


# ---- routes -------------------------------------------------------------

def _gap_row(callsign, gap):
    return {
        "callsign": callsign,
        "side": gap.get("side"),
        "known": gap.get("known"),
        "hint": gap.get("hint"),
        "chain": gap.get("chain"),
        "type": gap.get("type"),
        "n_recent": gap.get("n_recent"),
        "last_seen": gap.get("last_seen"),
        "last_heard": ({"lat": gap["last_lat"], "lon": gap["last_lon"],
                        "track": gap.get("last_trk")}
                       if gap.get("last_lat") is not None else None),
        "rotation_km": (gap.get("est_km")
                        if (gap.get("n_rot") or 0) >= MIN_ROTATIONS
                        else None),
    }


def _dark():
    return ApiError(503, "artifact_unavailable",
                    "the gaps artifact is not loaded",
                    headers={"Retry-After": "300"})


@router.get(
    "/v1/gaps", tags=["Contributions"], summary="Open questions",
    description="Callsigns whose route observation settled at one end "
                "only, most-seen first. Each row says which end is "
                "missing, which is known, where the aircraft was last "
                "heard, and how far its rotation says the other end is. "
                "Answers go to the contribution door, not this API. "
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
         offset: int = Query(0, ge=0),
         session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.gaps_rate_limit,
                       settings.rate_window_s, bucket="gaps")
    book = request.app.state.gaps
    if not book.available():
        raise _dark()
    answered = {cs for (cs,) in session.execute(
        select(RouteCatalog.callsign).where(RouteCatalog.valid_to.is_(None)))}
    rows, total = book.page(offset, limit,
                            airline=(airline or "").upper() or None,
                            side=side, exclude=answered)
    airports, network = _indexes(request)
    out = []
    for cs, g in rows:
        row = _gap_row(cs, g)
        row["suggested"] = candidates(session, airports, network, g, cs)
        out.append(row)
    response.headers["Cache-Control"] = CACHE
    return {"total": total, "offset": offset, "gaps": out,
            "coverage": "observed"}


def _indexes(request):
    state = request.app.state
    if not hasattr(state, "airport_index"):
        state.airport_index = AirportIndex()
    routes = state.routes
    network = routes.by_airline() if routes.available() else {}
    return state.airport_index, network


@router.get(
    "/v1/gaps/{callsign}", tags=["Contributions"], summary="One question",
    description="The open question for one callsign, the answers on "
                "file, and the catalog answer in force if there is one. "
                "404 when observation has no question for it. Rate: 300 "
                "per 600 s (bucket `gaps`). Cache: 10 min edge.",
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
        raise _dark()
    found = book.get(callsign)
    if found is None:
        raise ApiError(404, "not_found", "no open question")
    out = _gap_row(callsign, found)
    airports, network = _indexes(request)
    out["suggested"] = candidates(session, airports, network, found, callsign)
    current = catalog_current(session, callsign)
    out["catalog"] = ({"route": catalog_route(current),
                       "valid_from": str(current.valid_from)}
                      if current else None)
    out["answers"] = [
        {"origin": c.origin, "dest": c.dest, "status": c.status,
         "verdict": c.verdict}
        for c in session.execute(
            select(Claim).where(Claim.callsign == callsign,
                                Claim.status != "rejected")
            .order_by(Claim.first_at)).scalars()]
    response.headers["Cache-Control"] = CACHE
    return out


@router.get(
    "/v1/contributors", tags=["Contributions"], summary="Contributors",
    description="Who answered, by approved claims they stood behind, "
                "most first. Only claims in the catalog count, and only "
                "contributors who gave a name. Rate: 300 per 600 s "
                "(bucket `gaps`). Cache: 10 min edge.",
    operation_id="contributors",
    responses=spec.ok(spec.EX_CONTRIBUTORS, spec.R429,
                      schema=spec.SCH_CONTRIBUTORS),
    openapi_extra=spec.STABLE,
)
def contributors(request: Request, response: Response,
                 session=Depends(get_session)):
    settings = request.app.state.settings
    ratelimit.throttle(request, settings.gaps_rate_limit,
                       settings.rate_window_s, bucket="gaps")
    rows = session.execute(
        select(Endorsement.handle, func.count(func.distinct(Claim.id)),
               func.max(Claim.reviewed_at))
        .join(Claim, Claim.id == Endorsement.claim_id)
        .where(Claim.status == "approved", Endorsement.handle.is_not(None))
        .group_by(Endorsement.handle)
        .order_by(func.count(func.distinct(Claim.id)).desc(),
                  func.max(Claim.reviewed_at))
        .limit(200)).all()
    total = session.execute(
        select(func.count()).select_from(Claim)
        .where(Claim.status == "approved")).scalar_one()
    response.headers["Cache-Control"] = CACHE
    return {"answers": int(total),
            "contributors": [{"handle": h, "answers": int(n),
                              "latest": latest.date().isoformat()
                              if latest else None}
                             for h, n, latest in rows]}


# ---- review -------------------------------------------------------------

def approve(session, book, claim_id, valid_from=None, note=None):
    claim = session.get(Claim, claim_id)
    if claim is None or claim.status != "pending":
        raise ValueError("no pending claim %s" % claim_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    _approve(session, book, claim, now, by="operator", valid_from=valid_from,
             note=note)
    session.commit()
    return claim


def approve_clean(session, book, contributor, note=None):
    """Approve every pending claim a named contributor stands behind
    that no check disagrees with. Returns the claims approved."""
    now = datetime.datetime.now(datetime.timezone.utc)
    done = []
    for claim in session.execute(
            select(Claim).join(Endorsement, Endorsement.claim_id == Claim.id)
            .where(Claim.status == "pending",
                   or_(Endorsement.key_name == contributor,
                       Endorsement.handle == contributor))
            .distinct()).scalars().all():
        if claim.verdict == "contested" or disagrees(claim.checks):
            continue
        _approve(session, book, claim, now, by="operator", note=note)
        done.append(claim)
    session.commit()
    return done


def reject(session, claim_id, note=None):
    claim = session.get(Claim, claim_id)
    if claim is None or claim.status != "pending":
        raise ValueError("no pending claim %s" % claim_id)
    claim.status = "rejected"
    claim.reviewed_at = datetime.datetime.now(datetime.timezone.utc)
    claim.reviewed_by = "operator"
    claim.review_note = note
    session.commit()
    return claim


def withdraw(session, claim_id, note=None):
    """Undo an approval: the catalog row closes today as withdrawn and
    the claim is rejected, so the question reopens."""
    claim = session.get(Claim, claim_id)
    if claim is None or claim.status != "approved":
        raise ValueError("no approved claim %s" % claim_id)
    now = datetime.datetime.now(datetime.timezone.utc)
    for row in session.execute(
            select(RouteCatalog).where(RouteCatalog.claim_id == claim.id,
                                       RouteCatalog.valid_to.is_(None))
    ).scalars():
        row.valid_to = now.date()
        row.closed_reason = "withdrawn"
    claim.status, claim.reviewed_at = "rejected", now
    claim.reviewed_by, claim.review_note = "operator", note
    session.commit()
    return claim


def reconcile(session, routes_book, today=None):
    """Close catalog rows observation has overtaken: the same route now
    observed end to end, or a different one. Returns (callsign, reason)."""
    today = today or datetime.date.today()
    closed = []
    for row in session.execute(
            select(RouteCatalog).where(RouteCatalog.valid_to.is_(None))
    ).scalars():
        observed = routes_book.get(row.callsign)
        if not observed:
            continue
        same = observed == catalog_route(row)
        row.valid_to = today
        row.closed_reason = "observed" if same else "contradicted"
        closed.append((row.callsign, row.closed_reason))
    session.commit()
    return closed


def _print_claim(session, claim):
    print("#%-5d %-8s %s -> %s  %s  %s  %s" % (
        claim.id, claim.callsign, claim.origin, claim.dest, claim.status,
        claim.verdict or "-", claim.first_at.strftime("%Y-%m-%d")))
    print("       " + "  ".join("%s:%s" % kv
                                for kv in (claim.checks or {}).items()))
    for e in session.execute(select(Endorsement).where(
            Endorsement.claim_id == claim.id).order_by(Endorsement.id)
    ).scalars():
        who = " ".join(x for x in (e.handle, e.key_name and "[%s]" % e.key_name)
                       if x) or "anonymous"
        print("       %s%s%s" % (who, ": " + e.note if e.note else "",
                                 " from " + str(e.valid_from)
                                 if e.valid_from else ""))
    if claim.review_note:
        print("       review: %s" % claim.review_note)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull")
    p = sub.add_parser("show"); p.add_argument("id", type=int)
    p = sub.add_parser("approve"); p.add_argument("id", type=int, nargs="?")
    p.add_argument("--from", dest="valid_from", type=datetime.date.fromisoformat)
    p.add_argument("--note")
    p.add_argument("--contributor",
                   help="instead of one id: every pending claim this "
                        "contributor stands behind that nothing disagrees with")
    p = sub.add_parser("reject"); p.add_argument("id", type=int)
    p.add_argument("--note")
    p = sub.add_parser("withdraw"); p.add_argument("id", type=int)
    p.add_argument("--note")
    p = sub.add_parser("list"); p.add_argument("--approved", action="store_true",
                                               help="recent approvals instead")
    sub.add_parser("reconcile")
    sub.add_parser("propose")
    args = ap.parse_args(argv)

    from .gaps_db import GapBook
    from .legs_db import LegBook
    from .routes_db import RouteBook
    from .settings import Settings
    settings = Settings()
    session = make_sessionmaker(settings.database_url)()
    try:
        if args.cmd == "pull":
            if not settings.contribute_pull_url \
                    or not settings.contribute_pull_token:
                sys.exit("contribute pull is not configured")
            book = GapBook(settings.gaps_path)
            if not book.available():
                sys.exit("the gaps artifact is not loaded")
            counts = pull(session, book, lambda after: fetch_submissions(
                settings.contribute_pull_url, settings.contribute_pull_token,
                after), routes=RouteBook(settings.routes_path),
                legs=LegBook(settings.legs_path))
            print("pull: " + ", ".join("%s %d" % kv for kv in counts.items()))
            if counts["pending"] > settings.contribute_pending_alert \
                    or counts["received"] > settings.contribute_received_alert:
                sys.exit("contributions: pending %d, received %d"
                         % (counts["pending"], counts["received"]))
        elif args.cmd == "list":
            if args.approved:
                rows = session.execute(
                    select(Claim).where(Claim.status == "approved")
                    .order_by(Claim.reviewed_at.desc()).limit(40)
                ).scalars().all()
            else:
                rows = session.execute(
                    select(Claim).where(Claim.status == "pending")
                    .order_by(Claim.verdict, Claim.first_at)).scalars().all()
            for claim in rows:
                _print_claim(session, claim)
            print("%d %s" % (len(rows), "shown" if args.approved else "pending"))
        elif args.cmd == "show":
            claim = session.get(Claim, args.id)
            if claim is None:
                sys.exit("no claim %d" % args.id)
            _print_claim(session, claim)
        elif args.cmd == "approve":
            book = GapBook(settings.gaps_path)
            if args.contributor:
                done = approve_clean(session, book, args.contributor, args.note)
                for claim in done:
                    print("approved #%d %s %s -> %s" % (
                        claim.id, claim.callsign, claim.origin, claim.dest))
                print("%d approved for %s" % (len(done), args.contributor))
            elif args.id is None:
                ap.error("an id or --contributor")
            else:
                claim = approve(session, book, args.id, args.valid_from,
                                args.note)
                print("approved #%d %s %s -> %s" % (
                    claim.id, claim.callsign, claim.origin, claim.dest))
        elif args.cmd == "reject":
            claim = reject(session, args.id, args.note)
            print("rejected #%d %s" % (claim.id, claim.callsign))
        elif args.cmd == "withdraw":
            claim = withdraw(session, args.id, args.note)
            print("withdrawn #%d %s" % (claim.id, claim.callsign))
        elif args.cmd == "reconcile":
            closed = reconcile(session, RouteBook(settings.routes_path))
            for callsign, reason in closed:
                print("closed %s: %s" % (callsign, reason))
            print("%d closed" % len(closed))
        elif args.cmd == "propose":
            book = GapBook(settings.gaps_path)
            if not book.available():
                sys.exit("the gaps artifact is not loaded")
            filed, approved = propose(session, book,
                                      RouteBook(settings.routes_path),
                                      legs=LegBook(settings.legs_path))
            print("propose: filed %d, approved %d" % (filed, approved))
    finally:
        session.close()


if __name__ == "__main__":
    main()
