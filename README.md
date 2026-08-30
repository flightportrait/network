# FlightPortrait Network

FlightPortrait is an e-ink frame that draws the aircraft that flew
over your home.

We're building a network of feeders, open to anyone with an antenna.
The aircraft on the map, and on the wall, are what those receivers
heard.

Map: [flightportrait.com/network](https://flightportrait.com/network)
API: [data.flightportrait.com](https://data.flightportrait.com) (no API key)
Terms: [flightportrait.com/network/terms](https://flightportrait.com/network/terms)

## This repo

The map (`web/`), the API service we run (`api/`), [how to
feed](docs/feed.md), and [what we store about a
station](docs/privacy.md).

`web/` is static HTML. No build. It talks to our API. We serve it
at `/network/` on the site.

`api/` is the instance behind data.flightportrait.com, published so
the privacy handling is auditable ([its README](api/README.md)).
There is one network; this is the code it runs, not a self-host kit.

## Feed

If you already run a feeder, add this line next to the others:

```
adsb,feed.flightportrait.com,30004,beast_reduce_plus_out,uuid=YOUR-UUID
```

Keep the UUID. Starting from a Pi and a dongle:
[flightportrait.com/network/?mode=join](https://flightportrait.com/network/?mode=join).

Stations on the map are rounded to about 11 km. We do not store
feeder IPs.

## Licenses

Code here is Apache-2.0. Data from the API is ODbL 1.0.
Airline marks on the map belong to their owners. [NOTICE](NOTICE)
has the rest.
