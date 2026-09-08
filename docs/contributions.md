# Contributions

The network derives routes from what its receivers and the open trace
archives observed. Where coverage ends, so does the route: a flight
number seen leaving Singapore every day whose arrival end has no
receiver stays half known. Those are the gaps, and they are public:

- [flightportrait.com/network/gaps.html](https://flightportrait.com/network/gaps.html)
- `GET /v1/gaps`, `GET /v1/gaps/{callsign}`

## Answering one

Answers go to the door, not to the data API, which is read-only:

```
POST https://contribute.flightportrait.com/contributions
{"callsign": "SIA842", "dest": "TFU", "handle": "spotter_sg",
 "note": "daily, per the airline", "turnstile": "<token>"}
```

The page handles the Turnstile token. If you answer in volume, ask for
a key and send it as `X-Contribute-Key` instead. `handle` is the name
you want credit under; `valid_from` (a date) marks a schedule change.
The reply is `202 {"id": .., "status": "received"}` once the callsign
has an open question and the airport is a commercial field.

## What happens next

The network files each answer as a claim, with the people behind it,
and checks the claim against what was observed:

Where the rotation is known, each question also lists the airports the
evidence allows, best first: at that distance, within the aircraft's
range, along the last heard track, ranked by how much the airline is
seen flying there. Pick one, or type another.

| check | meaning |
|---|---|
| corridor | lies along the track the aircraft was last heard on |
| rotation | matches how far the airframe's time away says the other end is |
| type | within the aircraft type's range |
| mirror | agrees with what is known about the return flight |
| observation | agrees with the rare sighting of that end, when there was one |
| unique | the one airport the airline flies to that fits every filter |
| network | the airline is seen flying there elsewhere |
| keyed | distinct key holders who said the same |

When the filters leave exactly one airport the airline flies to, the
network files that answer itself each night and judges it like any
other; such claims carry the endorser `evidence`.

A claim with no failing check and two corroborating signals enters the
catalog on its own. A claim that fails a check is closed. Anything in
between waits for the operator. The flight's page then carries the
route with `route_source` `observed+catalog` (one end observed, one
from the catalog) or `catalog`. Observation always wins: when the
archive settles the route itself, the catalog row closes, and if the
two disagree the question reopens.

Contributors who gave a name are listed at
[flightportrait.com/network/contributors.html](https://flightportrait.com/network/contributors.html)
and `GET /v1/contributors`. Answers and catalog rows are open data
under the same licence as everything else here, ODbL 1.0.
