"""Estimated positions for aircraft that left the network's coverage.

When a cruising aircraft stops being heard, the map can keep drawing it
where it most likely is: flown on from its last observed position
toward the destination its callsign's route names, at its last observed
ground speed. Clearly labelled as estimated, served apart from observed
positions (/v1/estimated, never /v1/aircraft), never archived and never
exported: an estimate is a drawing aid, not data.

The module is pure: no I/O, no clock. The live book (EstimateBook)
feeds it observations and asks for positions; tools/estimate_backtest.py
feeds it recorded coverage gaps and scores it against where the
aircraft actually reappeared. The method and every gate below were
chosen on that backtest (docs: network/docs/estimates.md).
"""
import math

EARTH_KM = 6371.0088
KT_TO_KMS = 1.852 / 3600.0

# Who gets an estimate: cruising, fast, with a known destination still
# far ahead and roughly ahead. Chosen on the backtest.
MIN_ALT_FT = 18000
MIN_GS_KT = 250
MIN_REMAINING_KM = 250          # nearer than this it is descending to land
MAX_OFF_BEARING_DEG = 100       # destination behind the aircraft: unknown leg
# How long an estimate may run: never past the destination, never longer
# than the flight could plausibly take from here, and never beyond this.
# Backtest, 2026-09-22 traces: a slack of 1.25 drew 8 % of estimates
# for aircraft that had already landed, 1.1 none, at equal accuracy.
MAX_AGE_S = 8 * 3600
ETA_SLACK = 1.1
STOP_BEFORE_KM = 150            # stop drawing on final approach
# The converge method: the aircraft holds its last track for HOLD_S,
# then turns toward the destination at TURN_DEG_PER_MIN. Backtest: short
# gaps are won by holding the track (median 1.4 km at 5-15 min), long
# ones by heading for the destination (77 km at 30-120 min); pure dead
# reckoning erred 211 km on the long ones, a straight great circle 13 km
# on the short ones.
TURN_DEG_PER_MIN = 2.0
HOLD_S = 600.0
STEP_S = 30.0
# The live book: an aircraft counts as lost this long after its last
# position (the live sky drops it at 60 s).
LOST_AFTER_S = 90.0


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = (math.cos(p1) * math.sin(p2) -
         math.sin(p1) * math.cos(p2) * math.cos(dl))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def forward(lat, lon, heading_deg, dist_km):
    """The point dist_km away from (lat, lon) along an initial heading."""
    d = dist_km / EARTH_KM
    h = math.radians(heading_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) +
                   math.cos(p1) * math.sin(d) * math.cos(h))
    l2 = l1 + math.atan2(math.sin(h) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def angle_diff(a, b):
    """Signed smallest difference b - a, in degrees (-180, 180]."""
    d = (b - a + 180.0) % 360.0 - 180.0
    return 180.0 if d == -180.0 else d


def pick_destination(lat, lon, track, chain, airports):
    """The airport of a route chain the aircraft is flying toward: the
    first stop, in route order, that is ahead of it and far enough away.
    Stops behind it are legs already flown. None when no stop fits.
    airports: {code: (lat, lon)}."""
    for code in (chain or [])[1:]:
        ap = airports.get(code)
        if not ap:
            continue
        dist = haversine_km(lat, lon, ap[0], ap[1])
        off = abs(angle_diff(track, bearing_deg(lat, lon, ap[0], ap[1])))
        if off <= MAX_OFF_BEARING_DEG and dist >= MIN_REMAINING_KM:
            return code, ap[0], ap[1], dist
    return None


def eligible(obs):
    """obs: dict with lat, lon, alt (ft or 'ground'), gs (kt), track."""
    alt = obs.get("alt")
    return (isinstance(alt, (int, float)) and alt >= MIN_ALT_FT and
            (obs.get("gs") or 0) >= MIN_GS_KT and
            obs.get("track") is not None and
            obs.get("lat") is not None and obs.get("lon") is not None)


def project(obs, dest, dt_s, method="converge"):
    """Where the aircraft is dt_s after obs, flying toward dest
    (lat, lon). Returns (lat, lon, heading, remaining_km)."""
    lat, lon, hdg = obs["lat"], obs["lon"], obs["track"]
    kms = obs["gs"] * KT_TO_KMS
    if method == "dr":
        la, lo = forward(lat, lon, hdg, kms * dt_s)
        return la, lo, hdg, haversine_km(la, lo, dest[0], dest[1])
    if method == "gc":
        hdg = bearing_deg(lat, lon, dest[0], dest[1])
        total = haversine_km(lat, lon, dest[0], dest[1])
        step = min(kms * dt_s, total)
        la, lo = forward(lat, lon, hdg, step)
        rem = haversine_km(la, lo, dest[0], dest[1])
        hdg = bearing_deg(la, lo, dest[0], dest[1]) if rem > 1 else hdg
        return la, lo, hdg, rem
    # converge: fly the last track, turning toward the destination at a
    # bounded rate; one step at a time so the path is a smooth curve
    t, max_turn = 0.0, TURN_DEG_PER_MIN * STEP_S / 60.0
    while t < dt_s:
        step = min(STEP_S, dt_s - t)
        rem = haversine_km(lat, lon, dest[0], dest[1])
        if rem <= kms * step:
            return dest[0], dest[1], hdg, 0.0
        if t >= HOLD_S:
            want = bearing_deg(lat, lon, dest[0], dest[1])
            turn = angle_diff(hdg, want)
            hdg = (hdg + max(-max_turn, min(max_turn, turn))) % 360.0
        lat, lon = forward(lat, lon, hdg, kms * step)
        t += step
    return lat, lon, hdg, haversine_km(lat, lon, dest[0], dest[1])


def horizon_s(obs, remaining_km):
    """How long an estimate may run from obs: until the aircraft would be
    STOP_BEFORE_KM out, with slack for a slower real route, capped."""
    kms = max(obs["gs"], MIN_GS_KT) * KT_TO_KMS
    flying = max(0.0, remaining_km - STOP_BEFORE_KM) / kms
    return min(MAX_AGE_S, flying * ETA_SLACK)


class EstimateBook:
    """The live side: fed every published snapshot, asked for estimates.

    Keeps the last cruising observation of each aircraft; an aircraft
    that stops being heard is estimated from it until it is heard again,
    nears its destination, or outlives its horizon. Observations that
    are not cruising (climbing out, descending, on the ground) clear the
    aircraft: nothing is estimated for a flight about to land.
    routes(callsign) -> [IATA, ...] or None; airports() -> {IATA: (lat, lon)}.
    """

    def __init__(self, routes, airports):
        self._routes, self._airports = routes, airports
        self._last = {}               # hex -> obs
        self._live = set()            # hexes in the latest snapshot

    def observe(self, aircraft, now):
        live = set()
        for a in aircraft:
            hex_id = a.get("hex")
            if not hex_id or a.get("lat") is None:
                continue
            live.add(hex_id)
            obs = {"lat": a["lat"], "lon": a["lon"], "alt": a.get("alt_baro"),
                   "gs": a.get("gs"), "track": a.get("track")}
            if not eligible(obs):
                self._last.pop(hex_id, None)
                continue
            obs.update(hex=hex_id, flight=(a.get("flight") or "").strip(),
                       t=a.get("t"), r=a.get("r"), category=a.get("category"),
                       at=now - float(a.get("seen_pos") or 0.0), dest=None)
            self._last[hex_id] = obs
        self._live = live

    def estimates(self, now):
        out, airports = [], None
        for hex_id, obs in list(self._last.items()):
            if hex_id in self._live:
                continue
            dt = now - obs["at"]
            if dt > MAX_AGE_S:
                del self._last[hex_id]
                continue
            if dt < LOST_AFTER_S or not obs["flight"]:
                continue
            if obs["dest"] is None:
                airports = airports if airports is not None else self._airports()
                found = pick_destination(obs["lat"], obs["lon"], obs["track"],
                                         self._routes(obs["flight"]), airports)
                obs["dest"] = found or False
            if not obs["dest"]:
                continue
            code, dlat, dlon, dist = obs["dest"]
            if dt > horizon_s(obs, dist):
                del self._last[hex_id]
                continue
            lat, lon, hdg, rem = project(obs, (dlat, dlon), dt)
            if rem < STOP_BEFORE_KM:
                del self._last[hex_id]
                continue
            out.append({
                "hex": hex_id, "flight": obs["flight"], "t": obs["t"],
                "r": obs["r"], "category": obs["category"],
                "lat": round(lat, 4), "lon": round(lon, 4),
                "track": round(hdg, 1), "alt_baro": obs["alt"],
                "gs": obs["gs"], "estimated": True,
                "last_seen": {"at": round(obs["at"], 1),
                              "lat": obs["lat"], "lon": obs["lon"]},
                "destination": code,
                "eta": round(now + rem / (obs["gs"] * KT_TO_KMS)),
            })
        return out
