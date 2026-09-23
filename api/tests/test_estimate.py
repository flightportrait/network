"""Estimated positions: the geometry, the gates, and the live book's
lifecycle (lost, estimated, heard again, gone)."""
from app import estimate as E

AIRPORTS = {"LHR": (51.47, -0.4543), "JFK": (40.6413, -73.7781),
            "PSA": (43.6839, 10.3927), "CIA": (41.7994, 12.5949)}


def test_geometry():
    d = E.haversine_km(51.47, -0.4543, 40.6413, -73.7781)
    assert 5530 < d < 5560                               # LHR-JFK
    assert abs(E.bearing_deg(0, 0, 0, 10) - 90) < 1e-6
    lat, lon = E.forward(0, 0, 90, 111.19)
    assert abs(lat) < 1e-6 and abs(lon - 1.0) < 0.01
    assert E.angle_diff(350, 10) == 20 and E.angle_diff(10, 350) == -20


def test_destination_is_the_stop_ahead():
    # over Italy heading north-west: Pisa ahead, Rome behind
    found = E.pick_destination(41.0, 14.0, 315, ["CIA", "PSA"], AIRPORTS)
    assert found and found[0] == "PSA"
    # destination behind the aircraft: no guess
    assert E.pick_destination(41.0, 14.0, 135, ["CIA", "PSA"], AIRPORTS) is None
    # too close to the destination: it is landing
    assert E.pick_destination(43.5, 10.6, 315, ["CIA", "PSA"], AIRPORTS) is None


def test_converge_holds_then_turns_and_never_overshoots():
    obs = {"lat": 50.0, "lon": -20.0, "alt": 37000, "gs": 480.0, "track": 290.0}
    jfk = AIRPORTS["JFK"]
    # during the hold it is dead reckoning
    la, lo, hdg, _ = E.project(obs, jfk, 300)
    dla, dlo = E.forward(50.0, -20.0, 290.0, 480 * E.KT_TO_KMS * 300)
    assert E.haversine_km(la, lo, dla, dlo) < 0.5 and hdg == 290.0
    # after a long time it has turned toward the destination
    la, lo, hdg, rem = E.project(obs, jfk, 3 * 3600)
    assert abs(E.angle_diff(hdg, E.bearing_deg(la, lo, *jfk))) < 5
    # it arrives, not flies through
    assert E.project(obs, jfk, 20 * 3600)[3] == 0.0


def _ac(hex_id="4ca8e4", lat=42.2, lon=13.1, alt=36000, gs=452.0,
        track=312.0, flight="RYR1153", seen_pos=0.0):
    return {"hex": hex_id, "flight": flight, "t": "B738", "r": "9H-QDS",
            "lat": lat, "lon": lon, "alt_baro": alt, "gs": gs,
            "track": track, "seen_pos": seen_pos}


def test_book_lifecycle():
    book = E.EstimateBook(lambda cs: {"RYR1153": ["CFU", "PSA"]}.get(cs),
                          lambda: AIRPORTS)
    t0 = 1_000_000.0
    book.observe([_ac()], t0)
    assert book.estimates(t0 + 30) == []            # still heard
    book.observe([], t0 + 60)                        # gone from the sky
    assert book.estimates(t0 + 60) == []             # not lost yet
    [est] = book.estimates(t0 + 300)
    assert est["estimated"] is True and est["destination"] == "PSA"
    assert est["last_seen"] == {"at": t0, "lat": 42.2, "lon": 13.1}
    assert E.haversine_km(42.2, 13.1, est["lat"], est["lon"]) > 50
    assert est["eta"] > t0 + 300
    book.observe([_ac(lat=42.9, lon=12.2)], t0 + 400)   # heard again
    assert book.estimates(t0 + 410) == []
    # descending toward its destination: cleared, nothing estimated
    book.observe([_ac(alt=9000)], t0 + 500)
    book.observe([], t0 + 520)
    assert book.estimates(t0 + 900) == []


def test_book_gives_up_without_a_route_or_past_the_horizon():
    book = E.EstimateBook(lambda cs: None, lambda: AIRPORTS)
    book.observe([_ac()], 0.0)
    book.observe([], 60.0)
    assert book.estimates(600.0) == []               # no route: no guess
    book = E.EstimateBook(lambda cs: ["CFU", "PSA"], lambda: AIRPORTS)
    book.observe([_ac()], 0.0)
    book.observe([], 60.0)
    assert book.estimates(300.0)                     # en route (Pisa 275 km off)
    assert book.estimates(4 * 3600.0) == []          # long past its arrival


def test_endpoint_serves_estimates(ctx):
    client, app, sm, settings, readsb = ctx
    app.state.estimates = E.EstimateBook(lambda cs: ["CFU", "PSA"],
                                         lambda: AIRPORTS)
    import time
    now = time.time()
    app.state.estimates.observe([_ac()], now - 400)
    app.state.estimates.observe([], now - 340)
    body = client.get("/v1/estimated").json()
    assert body["method"] == "converge-to-destination"
    [a] = body["aircraft"]
    assert a["hex"] == "4ca8e4" and a["estimated"] is True
