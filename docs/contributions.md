# Contributions

The network derives routes from what its receivers and the open trace
archives observed. Where coverage ends, so does the route: a flight
number seen leaving Singapore every day whose arrival end has no
receiver stays half known. Those are the gaps, and they are public:

- [flightportrait.com/network/gaps.html](https://flightportrait.com/network/gaps.html)
- `GET /v1/gaps`, `GET /v1/gaps/{callsign}`

## Answering one

`POST /v1/contributions` with the callsign and the missing airport
(IATA or ICAO). The body is JSON:

```json
{"callsign": "SIA842", "dest": "TFU", "note": "daily, per the airline"}
```

`valid_from` (a date) marks a schedule change; `contact` is optional
and never published. The reply says what was checked:

| check | meaning |
|---|---|
| known_end | the end you gave for the settled side matches observation |
| airport | the missing end is a commercial airport in the registry |
| observation | agrees with the rare sighting of that end, when there was one |
| corridor | lies along the track the aircraft was last heard on |
| agreeing | earlier answers that say the same |

An answer that fails a check is still recorded; it is simply looked at
more closely. Nothing is served until the operator approves it.

## What happens next

An approved answer enters the catalog with the date it holds from.
The flight's page then carries the route with `route_source`
`observed+catalog` (one end observed, one from the catalog) or
`catalog`. Observation always wins: when coverage reaches the far end
and the archive settles the route itself, the catalog row closes, and
if the two disagree the question reopens.

Answers and catalog rows are open data under the same licence as
everything else here, ODbL 1.0.
