"""Reading a timetable from sightings: the methods the backtest weighs."""
import datetime
from array import array

from app.schedule_backtest import score
from app.schedule_pick import pick

DAY = datetime.date(2026, 9, 20).toordinal()     # a Sunday


def _at(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def test_mode_is_the_most_seen_slot_ties_earliest():
    seen = [(DAY - 3, _at("09:30")), (DAY - 2, _at("09:31")),
            (DAY - 1, _at("09:47"))]
    assert pick(seen, DAY, "mode") == _at("09:30")
    assert pick([(DAY, _at("10:05")), (DAY, _at("09:50"))], DAY, "mode") \
        == _at("09:50")
    assert pick([], DAY, "mode") is None


def test_recent_follows_a_retimed_flight():
    """Forty days at 08:00, then retimed to 10:00 ten days ago: the plain
    mode still says 08:00, the recency-weighted one has moved."""
    seen = [(DAY - d, _at("08:00")) for d in range(11, 51)] + \
           [(DAY - d, _at("10:00")) for d in range(1, 11)]
    assert pick(seen, DAY, "mode") == _at("08:00")
    assert pick(seen, DAY, "recent") == _at("10:00")


def test_kernel_prefers_a_cluster_to_one_lucky_slot():
    """Take-offs scatter across 08:05-08:14; three stray evening ones
    share a single slot. The cluster is the schedule."""
    seen = [(DAY - d, m) for d, m in enumerate(
        [_at("08:06"), _at("08:08"), _at("08:11"), _at("08:13"),
         _at("14:00"), _at("14:01"), _at("14:03")], start=1)]
    assert pick(seen, DAY, "mode") == _at("14:00")
    assert pick(seen, DAY, "kernel") == _at("08:10")


def test_weekday_keeps_a_different_friday():
    seen = []
    for d in range(1, 43):
        day = DAY - d
        friday = datetime.date.fromordinal(day).weekday() == 4
        seen.append((day, _at("18:00") if friday else _at("07:00")))
    assert pick(seen, DAY, "weekday", weekday=4) == _at("18:00")
    assert pick(seen, DAY, "weekday", weekday=1) == _at("07:00")
    assert pick(seen, DAY, "mode", weekday=4) == _at("07:00")


def test_backtest_predicts_each_day_from_the_days_before():
    """A daily flight retimed three weeks ago: the methods
    that weigh recency score, the plain mode does not; the first
    sighting of a new number has nothing to be predicted from."""
    legs = {("SIA826", "SIN", "PEK"): array("i", [
        (DAY - d) * 1440 + (_at("01:00") if d > 20 else _at("02:30"))
        for d in range(0, 60)])}
    legs[("SIA999", "SIN", "NRT")] = array("i", [DAY * 1440 + _at("09:00")])
    res = score(legs, days=3, window=60, methods=("mode", "recent"))
    assert res["flown"] == 4 and res["unseen"] == 1
    assert res["methods"]["recent"]["hits"][45] == 3
    assert res["methods"]["mode"]["hits"][45] == 0
