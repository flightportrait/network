"""How a flight's scheduled time is read from what the network saw.

One flight number on one leg leaves at roughly the same local time on
most days; the schedule is that time. The ways of reading it, which the
backtest (schedule_backtest) scores side by side before the nightly
derive takes one:

- mode: the most-seen 5-minute slot, ties to the earliest (the original
  derive, and what `refdata_ingest schedule` still writes).
- recent: each sighting weighs by its age, halving every HALF_LIFE_DAYS,
  so a retimed flight takes its new slot after about one half-life
  instead of once the new slot outnumbers the old one in the window.
- kernel: the slot with the most sightings within KERNEL_MIN of it,
  then the median of those sightings: take-off times scatter across
  neighbouring slots, and one lucky bin should not beat a cluster.
- weekday: when the flight has enough sightings on the asked weekday
  and they sit in another slot (more than WEEKDAY_APART_MIN from the
  pooled time), that weekday's own time (some numbers keep a different slot on
  Fridays or weekends).

A method is a "+"-joined set of these: "recent+kernel+weekday".
Sightings are (day ordinal, local minute of day); minutes are circular.
"""

HALF_LIFE_DAYS = 7.0
KERNEL_MIN = 15
WEEKDAY_MIN_SEEN = 3
WEEKDAY_APART_MIN = 30
SLOT = 5

METHODS = ("mode", "recent", "kernel", "recent+kernel", "weekday",
           "recent+kernel+weekday")


def circ_diff(a, b):
    return abs(((a - b + 720) % 1440) - 720)


def _weights(seen, asof, recent):
    if not recent:
        return [1.0] * len(seen)
    return [0.5 ** (max(0, asof - day) / HALF_LIFE_DAYS) for day, _ in seen]


def _pick(seen, asof, recent, kernel):
    weights = _weights(seen, asof, recent)
    slots = {}
    for (_, minute), w in zip(seen, weights):
        s = (minute // SLOT) * SLOT
        slots[s] = slots.get(s, 0.0) + w
    if not kernel:
        return min(slots, key=lambda s: (-round(slots[s], 9), s))

    def mass(s):
        return sum(v for t, v in slots.items() if circ_diff(s, t) <= KERNEL_MIN)
    best = min(slots, key=lambda s: (-round(mass(s), 9), -round(slots[s], 9), s))
    near = sorted(((m - best + 720) % 1440 - 720, w)
                  for (_, m), w in zip(seen, weights)
                  if circ_diff(m, best) <= KERNEL_MIN)
    half, acc = sum(w for _, w in near) / 2, 0.0
    for offset, w in near:
        acc += w
        if acc >= half:
            break
    minute = (best + offset) % 1440
    return int(round(minute / SLOT) * SLOT) % 1440


def pick(seen, asof, method="mode", weekday=None):
    """The scheduled local minute from sightings [(day ordinal, minute)],
    as of day ordinal `asof` (sightings after it are the caller's to
    leave out). `weekday` (0 = Monday) is the day asked about, for the
    weekday method. None when there is nothing to read."""
    if not seen:
        return None
    flags = set(method.split("+"))
    recent, kernel = "recent" in flags, "kernel" in flags
    pooled = _pick(seen, asof, recent, kernel)
    if "weekday" in flags and weekday is not None:
        same = [s for s in seen if (s[0] - 1) % 7 == weekday]
        # date.toordinal(): day 1 is a Monday, so (ordinal - 1) % 7 is
        # Python's weekday()
        if len(same) >= WEEKDAY_MIN_SEEN:
            own = _pick(same, asof, recent, kernel)
            # a weekday's few sightings are noisier than the pool: its
            # own time wins only when it is a different slot altogether
            if circ_diff(own, pooled) > WEEKDAY_APART_MIN:
                return own
    return pooled
