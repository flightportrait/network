# Privacy

What we store about a station. These are not settings.

The UUID in the Beast line is the key to
`GET /v1/stations/{uuid}`. Malformed, unknown, and wrong UUIDs all
return 404. The server stores a SHA-256 of the UUID, not the UUID.
The public roster shows a generated id (`fp-` plus a short hash
prefix).

Locations are the midpoint of coverage the station hears, rounded
to 0.1 degree (about 11 km). We do not store an address or
feeder-supplied coordinates.

The feeder IP holds the TCP connection and is discarded when that
table is parsed. It is not written to the registry.

The roster (`GET /v1/stations`): generated id, optional label,
coarse coordinates, first and last heard, online or not.

To delete a station's registry history, email
hello@flightportrait.com. There is no public delete endpoint. Data
already published under ODbL stays.

`web/` is static files. It talks to the API, OpenFreemap, and
(in the browser) planespotters.net. It does not receive Beast
connections.
