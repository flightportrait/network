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
