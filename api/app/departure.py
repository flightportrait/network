"""When did this aircraft take off, and has it landed: read from the
day's trace the hub keeps (readsb's trace_full, one file per aircraft
and day).

The current flight is the last stretch of the trace: back from its end
until a gap of twenty minutes or a point on the ground. The first
airborne point of that stretch is the departure when the stretch
starts on the ground, or low enough that we plainly heard the take-off;
an aircraft first heard at cruise has no departure we can vouch for.
A stretch that ends on the ground after flying has landed."""

GAP_S = 1200.0          # a silence this long separates two flights
LOW_FT = 5000           # first heard below this: the take-off was ours


def _rows(trace: dict) -> list:
    t0 = trace.get("timestamp")
    out = []
    if not isinstance(t0, (int, float)):
        return out
    for p in trace.get("trace") or []:
        if isinstance(p, list) and len(p) >= 4 and isinstance(p[0], (int, float)):
            out.append((t0 + p[0], p[1], p[2], p[3]))
    return out


def flight_bounds(trace: dict) -> dict | None:
    rows = _rows(trace)
    if not rows:
        return None
    i = len(rows) - 1
    while i > 0:
        t, _, _, alt = rows[i]
        tp, _, _, altp = rows[i - 1]
        if t - tp > GAP_S:
            break
        if altp == "ground" and alt != "ground":
            break
        i -= 1
    first = rows[i]
    seg = rows[i:]
    out = {"departure": None, "arrival": None}
    airborne = [r for r in seg if r[3] != "ground"]
    if not airborne:
        return out
    take = airborne[0]
    from_ground = i > 0 and rows[i - 1][3] == "ground" or first[3] == "ground"
    alt = take[3] if isinstance(take[3], (int, float)) else None
    if from_ground or (alt is not None and alt <= LOW_FT):
        out["departure"] = {"at": round(take[0], 1), "lat": take[1],
                            "lon": take[2], "alt_ft": alt if alt is not None else 0}
    last = seg[-1]
    if last[3] == "ground" and airborne:
        # the first ground point after the last airborne one
        j = len(seg) - 1
        while j > 0 and seg[j - 1][3] == "ground":
            j -= 1
        land = seg[j]
        out["arrival"] = {"at": round(land[0], 1), "lat": land[1], "lon": land[2]}
    return out
