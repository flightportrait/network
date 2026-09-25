"""Emergency squawks become airframe events only when they hold; 7500
is recorded but held from public view."""
from sqlalchemy import select

from app import squawks
from app.refdata_models import AirframeEvent, AirframeSpell


def _line(code="7700", hex_id="49d283", **kw):
    return {"hex": hex_id, "squawk": code, "flight": "TVS2221 ",
            "lat": 50.1, "lon": 14.2, "alt_baro": 35000, **kw}


def test_a_signal_counts_only_once_it_holds():
    w = squawks.SquawkWatcher()
    w.observe(_line(), 1000.0)
    w.observe(_line(), 1010.0)            # two reports, only 10 s apart
    assert w.drain() == []
    w.observe(_line(), 1021.0)            # held 21 s
    events = w.drain()
    assert len(events) == 1
    ev = events[0]
    assert ev["at"] == 1000.0 and ev["visibility"] == "public"
    assert ev["detail"] == {"code": "7700", "emergency": None,
                            "callsign": "TVS2221", "alt_baro": 35000}
    w.observe(_line(), 1100.0)            # same episode: nothing new
    assert w.drain() == []


def test_one_frame_and_ordinary_codes_are_nothing():
    w = squawks.SquawkWatcher()
    w.observe(_line(), 0.0)               # a lone mis-decode
    w.observe(_line("1000"), 30.0)
    w.observe(_line("2000", emergency="none"), 60.0)
    assert w.drain() == []


def test_a_new_episode_after_silence_and_7500_is_held():
    w = squawks.SquawkWatcher()
    for t in (0.0, 25.0):
        w.observe(_line("7600"), t)
    for t in (1000.0, 1030.0):            # > EPISODE_GAP_S later
        w.observe(_line("7500"), t)
    for t in (0.0, 25.0):
        w.observe(_line("2000", hex_id="4ca123", emergency="unlawful"), t)
    events = w.drain()
    assert [(e["detail"]["code"], e["visibility"]) for e in events] == [
        ("7600", "public"), ("7500", "held"), (None, "held")]


def test_written_events_attach_to_the_record(ctx):
    client, app, sm, settings, readsb = ctx
    w = squawks.SquawkWatcher()
    for t in (1790000000.0, 1790000030.0):
        w.observe(_line(), t)
    events = w.drain()
    assert squawks.write_events(sm, events) == 1
    assert squawks.write_events(sm, events) == 0      # idempotent
    with sm() as session:
        spell = session.execute(select(AirframeSpell)).scalar_one()
        assert (spell.kind, spell.value, spell.source) == \
            ("hex", "49d283", "live")
        ev = session.execute(select(AirframeEvent)).scalar_one()
        assert ev.kind == "squawk" and ev.airframe_id == spell.airframe_id
        assert ev.detail["code"] == "7700"
    # no registry row and no log: the record alone still answers
    body = client.get("/v1/airframes/49d283").json()
    assert body["reg"] is None
    assert [e["kind"] for e in body["history"]["events"]] == ["squawk"]


def test_watcher_can_be_turned_off(monkeypatch):
    # where another process records squawks from the same sky
    from app.settings import Settings
    monkeypatch.setenv("NETWORK_API_SQUAWK_WATCHER", "off")
    assert Settings().squawk_watcher is False
    monkeypatch.setenv("NETWORK_API_SQUAWK_WATCHER", "on")
    assert Settings().squawk_watcher is True
    monkeypatch.delenv("NETWORK_API_SQUAWK_WATCHER")
    assert Settings().squawk_watcher is True
