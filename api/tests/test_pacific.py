"""The fixed Pacific routes: loaded from the FAA-derived file, matched
to an aircraft flying (or joining) one, and scored over an ocean gap."""
from app import estimate as E
from app import pac_score as P
from app import pacific

LINE = [(20.0, -150.0), (25.0, -140.0), (30.0, -130.0)]
AWY = {"T1": LINE}


def _on(frac, a, b, off_km=0.0):
    brg = E.bearing_deg(*a, *b)
    la, lo = E.forward(*a, brg, E.haversine_km(*a, *b) * frac)
    if off_km:
        la, lo = E.forward(la, lo, brg + 90, off_km)
    return la, lo


def test_the_routes_are_placed():
    a = pacific.airways()
    assert len(a) >= 70
    # R576: Maui to the California coast
    assert a["R576"][0] == (20.90647, -156.42095)
    assert abs(a["R576"][-1][1] + 122.58) < 0.01
    # R220 runs from Alaska across 180 to 160 east
    assert a["R220"][0][1] < -150 and a["R220"][-1][1] > 155


def test_on_the_route_both_ways():
    la, lo = _on(0.5, LINE[0], LINE[1])
    fwd = {"lat": la, "lon": lo, "track": E.bearing_deg(la, lo, *LINE[1])}
    assert E.match_airway(fwd, AWY) == LINE[1:]
    back = dict(fwd, track=E.bearing_deg(la, lo, *LINE[0]))
    assert E.match_airway(back, AWY) == [LINE[0]]
    # 100 km to the side: not on it
    la, lo = _on(0.5, LINE[0], LINE[1], off_km=100)
    assert E.match_airway(dict(fwd, lat=la, lon=lo), AWY) is None
    # crossing it at right angles: not flying it
    assert E.match_airway(dict(fwd, track=(fwd["track"] + 90) % 360), AWY) is None


def test_heading_for_a_route_is_not_on_it():
    # 400 km short of the first point, aimed at it: no match (joining
    # was tried and dropped, see estimate.AIRWAY_*)
    la, lo = E.forward(*LINE[0], E.bearing_deg(*LINE[1], *LINE[0]), 400)
    obs = {"lat": la, "lon": lo, "track": E.bearing_deg(la, lo, *LINE[0])}
    assert E.match_airway(obs, AWY) is None


def test_a_route_away_from_the_destination_does_not_count():
    la, lo = _on(0.5, LINE[0], LINE[1])
    obs = {"lat": la, "lon": lo, "track": E.bearing_deg(la, lo, *LINE[1])}
    assert E.match_airway(obs, AWY, dest=(33.94, -118.41)) == LINE[1:]
    assert E.match_airway(obs, AWY, dest=(21.32, -157.92)) is None


def test_score_flies_the_route_across_the_gap():
    # heard on the route, lost for 3 hours, heard again further along it
    t0 = 1_758_000_000
    la0, lo0 = _on(0.2, LINE[0], LINE[1])
    trk = E.bearing_deg(la0, lo0, *LINE[1])
    obs = {"lat": la0, "lon": lo0, "track": trk, "gs": 480.0}
    path = LINE[1:] + [(33.94, -118.41)]
    la1, lo1, trk1, _ = E.project_path(obs, path, 3 * 3600)
    trace = {"timestamp": t0, "trace": [
        [0, 21.0, -151.5, 36000, 480.0, trk, 0, None, {"flight": "UAL1"}],
        [60, la0, lo0, 36000, 480.0, trk, 0, None, None],
        [60 + 3 * 3600, la1, lo1, 36000, 480.0, trk1, 0, None, None]]}
    counts, rows = P.score([("hawaii", trace)], lambda cs: ["HNL", "LAX"],
                           {"HNL": (21.32, -157.92), "LAX": (33.94, -118.41)}, AWY)
    assert counts["gaps"] == 1 and counts["matched"] == 1
    [r] = rows
    assert r["airway_km"] < 1.0 and r["current_km"] > r["airway_km"]
    doc = P.summary(counts, rows)
    assert doc["by_kind"]["hawaii"]["n"] == 1 and doc["by_gap"]["3-6h"]["n"] == 1


def test_kind_of_crossing():
    def tr(*pts):
        return {"trace": [[i * 60, la, lo, 36000] for i, (la, lo) in enumerate(pts)]}
    assert P.kind(tr((35.5, 140.0), (60.0, -150.0))) == "transpacific"
    assert P.kind(tr((21.0, -157.0), (34.0, -120.0))) == "hawaii"
    assert P.kind(tr((-33.9, 151.2), (-20.0, -170.0), (34.0, -120.0))) == "south"
    assert P.kind(tr((51.0, -15.0), (53.0, -50.0))) is None


def test_a_route_that_ends_nearer_but_is_not_the_way():
    # 2026-09-22: LAX-HNL flights leaving California sat on B200, which
    # runs south to the equator; its end is nearer Honolulu than the
    # aircraft, but flying it would be a long detour
    b200 = pacific.airways()["B200"]
    la, lo = _on(0.1, b200[0], b200[1])
    obs = {"lat": la, "lon": lo, "track": E.bearing_deg(la, lo, *b200[1])}
    assert E.match_airway(obs, {"B200": b200}) is not None
    assert E.match_airway(obs, {"B200": b200}, dest=(21.32, -157.92)) is None
