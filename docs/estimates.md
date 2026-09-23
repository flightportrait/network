# Estimated positions

The network hears an aircraft only while one of its stations is in
range. Over the sea, and anywhere between stations, a cruising aircraft
drops out of the sky for minutes or hours. The map keeps drawing it
where it most likely is, and says plainly that this position is
estimated.

## What gets an estimate

An aircraft that was last heard cruising:

- at or above 18,000 ft and 250 kt,
- with a callsign whose route the network knows,
- with the next stop of that route at least 250 km ahead of it and
  roughly in front of it (within 100 degrees of its track).

Climbing, descending, slow or unrouted aircraft get none: nothing is
guessed for a flight that is about to land or whose destination is
unknown.

The route comes from the observed routes first, then the community
catalog, then the inferred schedule: a flight number flown on one leg
is that leg, legs that link end to end are the whole chain, and a
number flown on several legs gives its main leg only when at least 70 %
of its flights fly it. Adding the schedule gave 11 % more lost aircraft
an estimate at unchanged accuracy (backtest, 4,910 traces).

## How it is placed

From the last observed position, at the last observed ground speed:

1. the aircraft holds its last track for 10 minutes,
2. then turns toward the destination at 2 degrees a minute and flies
   the great circle to it.

It appears 90 seconds after the aircraft was last heard, and disappears
when the aircraft is heard again, when it would be 150 km from its
destination, or when its remaining flight time (plus 10 %) has run out.
Altitude and speed shown are the last observed ones.

## How well it works

`tools/estimate_backtest.py` replays recorded traces: every gap in a
trace is an aircraft that left coverage and came back, so the estimate
at the moment it reappeared can be compared with where it really was.

On the network's traces of 2026-09-22 (3,665 gaps, 213 that qualified
for an estimate):

| Gap length | Median error |
|---|---|
| 5 to 15 minutes | 1 km |
| 15 to 30 minutes | 16 km |
| 1 to 2 hours | 77 km |

No estimate was drawn for an aircraft that had actually landed. Holding
the track alone erred 194 km on the long gaps; flying straight to the
destination erred 13 km on the short ones; the combination wins both.
Airways, holding patterns and weather reroutes are not modelled, so
long estimates carry tens of kilometres of error by design.

## Over the North Atlantic

Most transatlantic flights fly the day's organised tracks, published
twice a day by Shanwick and Gander: an entry point on one side of the
ocean, a position every 10 degrees of longitude, an exit point on the
other. `app/nat.py` keeps every message; the entry and exit points are
placed from `refdata/nat_fixes.csv`, built by `tools/nat_fixes.py` from
the FAA's NASR data (Gander's side) and the UK and Irish AIPs
(Shanwick's side). An aircraft heading onto a track at one of its
levels can be flown along it instead of the great circle.
`app/nat_score.py` scores that every night against ocean crossings
from the adsb.lol archive; the map uses it only once those scores show
it placing aircraft better.

## On the map

Estimated aircraft are drawn with the same silhouettes as heard ones,
in grey ink instead of the sky's red, with "est" beside the callsign.
Their card says when the aircraft was last heard, where it is heading
and when it should arrive, and that the position is placed, not
observed. They never enter the counts or the list of aircraft in view.
The dashed-circle button hides them (remembered per browser), and
playback hides them: an estimate belongs to the present.

## What it is not

An estimate is a drawing aid, not data. Estimated positions are served
by `/v1/estimated` only, always flagged `estimated: true` with the last
real position beside them. They never enter `/v1/aircraft`, the counts,
the flight logs, the archive or any export.
