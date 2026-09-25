"""The North Atlantic track message: parsed from the real 2026-09-23
message, kept once, the valid set picked by time."""
import datetime
import json
import os

from app import nat

HERE = os.path.dirname(os.path.abspath(__file__))


def _parts():
    with open(os.path.join(HERE, "data", "nat_2026-09-23.json")) as fh:
        return json.load(fh)


def test_points():
    assert nat.parse_point("54/20") == (54.0, -20.0)
    assert nat.parse_point("5630/40") == (56.5, -40.0)
    assert nat.parse_point("MALOT") is None
    assert nat.parse_point("99/20") is None


def test_the_real_message():
    [msg] = nat.parse_parts(_parts())
    assert (msg["issuer"], msg["tmi"]) == ("EGGX", 266)
    assert msg["valid_from"] == "2026-09-23T11:30:00Z"
    letters = [t["letter"] for t in msg["tracks"]]
    assert letters == list("ABCDEFG")
    c = msg["tracks"][2]
    assert (c["entry"], c["exit"]) == ("MALOT", "LOMSI")
    assert c["points"] == [[54.0, -20.0], [56.0, -30.0], [56.5, -40.0], [55.0, -50.0]]
    assert c["direction"] == "W" and c["west_levels"][0] == 340
    assert c["east_levels"] == []


def test_active_by_time():
    msgs = nat.parse_parts(_parts())
    utc = datetime.timezone.utc
    assert len(nat.active(msgs, datetime.datetime(2026, 9, 23, 12, tzinfo=utc))) == 7
    assert nat.active(msgs, datetime.datetime(2026, 9, 23, 20, tzinfo=utc)) == []


def test_store_keeps_each_message_once(ctx):
    client, app, sm, settings, readsb = ctx
    from app.refdata_models import NatMessage
    msgs = nat.parse_parts(_parts())
    assert nat.store(sm, msgs) == 1
    assert nat.store(sm, msgs) == 0
    with sm() as session:
        row = session.query(NatMessage).one()
        assert row.tmi == 266 and len(row.tracks) == 7
        assert "C MALOT 54/20" in row.raw


def _tracks():
    from app import nat as N
    msgs = N.parse_parts(_parts())
    utc = datetime.timezone.utc
    return N.active(msgs, datetime.datetime(2026, 9, 23, 13, tzinfo=utc))


def test_match_picks_the_track_ahead():
    from app import estimate as E
    tracks = _tracks()
    # west of Ireland at FL360, aimed at MALOT: track C's oceanic entry
    la, lo = 53.2, -10.0
    obs = {"lat": la, "lon": lo, "alt": 36000, "gs": 480.0,
           "track": E.bearing_deg(la, lo, 53.0, -15.0)}
    m = E.match_nat(obs, tracks)
    lomsi = nat.fixes()["LOMSI"]
    assert m and m[0] == (53.0, -15.0) and m[1] == (54.0, -20.0)
    assert m[-2] == (55.0, -50.0) and m[-1] == lomsi
    # eastbound: today's tracks are westbound only
    assert E.match_nat(dict(obs, track=80.0), tracks) is None
    # below the track levels: a random route, no track claim
    assert E.match_nat(dict(obs, alt=30000), tracks) is None


def test_on_the_track_over_the_ocean():
    from app import estimate as E
    tracks = _tracks()
    # on track C between 30W and 40W, heading west
    obs = {"lat": 56.2, "lon": -35.0, "alt": 37000, "gs": 470.0, "track": 272.0}
    m = E.match_nat(obs, tracks)
    assert m == [(56.5, -40.0), (55.0, -50.0), nat.fixes()["LOMSI"]]


def test_project_path_follows_the_waypoints():
    from app import estimate as E
    obs = {"lat": 54.0, "lon": -20.0, "alt": 36000, "gs": 480.0, "track": 290.0}
    pts = [(56.0, -30.0), (56.5, -40.0)]
    leg1 = E.haversine_km(54.0, -20.0, 56.0, -30.0)
    secs = leg1 / (480 * E.KT_TO_KMS)
    la, lo, _, rem = E.project_path(obs, pts, secs)
    assert E.haversine_km(la, lo, 56.0, -30.0) < 1.0
    assert abs(rem - E.haversine_km(56.0, -30.0, 56.5, -40.0)) < 1.0
    assert E.project_path(obs, pts, 10 * 3600)[3] == 0.0


def test_named_ends_are_placed():
    f = nat.fixes()
    # Shannon's oceanic entry points sit on 15W; Gander's on the coast
    assert f["MALOT"] == (53.0, -15.0) and f["DINIM"] == (51.0, -15.0)
    assert f["RATSU"] == (61.0, -10.0) and f["NALAN"] == (61.0, -6.0)
    assert abs(f["LOMSI"][0] - 53.1) < 1e-6 and abs(f["LOMSI"][1] + 56.7833) < 1e-3
    [msg] = nat.parse_parts(_parts())
    assert nat.unknown_fixes([msg]) == []
    c = msg["tracks"][2]
    path = nat.track_path(c)
    assert path[0] == (53.0, -15.0) and path[1] == (54.0, -20.0)
    assert path[-1] == f["LOMSI"] and len(path) == len(c["points"]) + 2
    # a fix the table does not know: that end is simply left off
    odd = dict(c, entry="ZZZZZ")
    assert nat.track_path(odd)[0] == (54.0, -20.0)
    assert nat.unknown_fixes([{"tracks": [odd]}]) == ["ZZZZZ"]


def test_fix_table_parsers():
    import sys
    sys.path.insert(0, os.path.join(HERE, "..", "..", "tools"))
    import nat_fixes as F
    assert F.dms("513000N") == 51.5 and F.dms("0150000W") == -15.0
    assert abs(F.dms("525225.50N") - (52 + 52 / 60 + 25.5 / 3600)) < 1e-9
    uk = (b"<td>RATSU</td><td>TDESIGNATED_POINT;CODE_ID;735</td>"
          b"<td>610000N</td><td>TDESIGNATED_POINT;GEO_LAT;735</td>"
          b"<td>0100000W</td><td>TDESIGNATED_POINT;GEO_LONG;735</td>")
    assert F.aip(uk, F._UK) == [("RATSU", 61.0, -10.0)]
    ie = b"<tr><td>MALOT</td><td>530000N 0150000W</td><td>Oceanic Entry</td></tr>"
    assert F.aip(ie, F._IE) == [("MALOT", 53.0, -15.0)]
    assert F.inside(53.0, -15.0) and not F.inside(48.3, 35.4)


def test_collector_can_be_turned_off(monkeypatch):
    # where another process keeps the same messages
    from app.settings import Settings
    monkeypatch.setenv("NETWORK_API_NAT_COLLECT", "off")
    assert Settings().nat_collect is False
    monkeypatch.delenv("NETWORK_API_NAT_COLLECT")
    assert Settings().nat_collect is True
