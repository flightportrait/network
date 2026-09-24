"""Find my station: a Station waiting for setup, found from the join page.

A freshly flashed Station image has no screen and no terminal, and its
setup page is on the home network at an address the person does not
know. While it waits for setup, the Station reports that address here
(POST /v1/setup/beacon, about once a minute); the join page, opened on a
phone on the same network, asks for it (GET /v1/setup/beacon). Both
reach us from the same public address, and that is the pairing.

Held in memory only, for BEACON_TTL_S after the last report, under a
salted hash of the public address (an IPv6 address counts by its /64,
the home's network). Nothing is written anywhere and nothing survives a
restart. Only private IPv4 addresses on the home network are accepted,
and a network is only ever answered with its own stations.
"""
import hashlib
import ipaddress
import re
import secrets
import time

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from . import openapi as spec
from . import ratelimit
from .errors import ApiError

BEACON_TTL_S = 600
MAX_PER_NETWORK = 8
_SALT = secrets.token_bytes(16)             # per process: memory only
_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_book = {}                                  # network -> {lan: (port, name, seen)}

router = APIRouter(tags=["Stations"])


def network_of(ip: str) -> str:
    """The key a home is known by: its IPv4 address, or its IPv6 /64."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return hashlib.sha256(_SALT + ip.encode()).hexdigest()
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.version == 6:
        key = str(ipaddress.ip_network("%s/64" % addr, strict=False))
    else:
        key = str(addr)
    return hashlib.sha256(_SALT + key.encode()).hexdigest()


def lan_ok(lan: str) -> bool:
    """A private IPv4 address a phone on the same network can open."""
    try:
        addr = ipaddress.ip_address(lan)
    except ValueError:
        return False
    return (addr.version == 4 and addr.is_private and not addr.is_loopback
            and not addr.is_link_local and not addr.is_unspecified)


def _sweep(now: float) -> None:
    for net in list(_book):
        entries = _book[net]
        for lan in [k for k, v in entries.items() if now - v[2] > BEACON_TTL_S]:
            del entries[lan]
        if not entries:
            del _book[net]


def reset() -> None:
    """Tests only: the book is process-global."""
    _book.clear()


class Beacon(BaseModel):
    lan: str
    port: int = 8654
    name: str = "station"


@router.post(
    "/v1/setup/beacon", summary="Station waiting for setup",
    description="Sent by a Station that has not been set up yet: its "
                "address on the home network, so the join page on a phone "
                "on that network can open its setup page. Held in memory "
                "for 10 minutes under a hash of the caller's public "
                "address; never stored. Only private IPv4 addresses are "
                "accepted. Rate: 30 per 600 s (bucket `beacon`).",
    operation_id="setup_beacon_post", status_code=204,
    responses={**spec.R429, **spec.R422},
    openapi_extra=spec.MAP_TIER,
)
def report(beacon: Beacon, request: Request):
    settings = request.app.state.settings
    ratelimit.throttle(request, 30, settings.rate_window_s, bucket="beacon")
    name = beacon.name.strip().lower()
    if not lan_ok(beacon.lan) or not 1 <= beacon.port <= 65535 \
            or not _NAME.match(name):
        raise ApiError(422, "invalid_request",
                       "lan must be a private IPv4 address, port 1-65535, "
                       "name a hostname")
    now = time.monotonic()
    _sweep(now)
    entries = _book.setdefault(network_of(ratelimit.client_ip(request)), {})
    if beacon.lan not in entries and len(entries) >= MAX_PER_NETWORK:
        oldest = min(entries, key=lambda k: entries[k][2])
        del entries[oldest]
    entries[beacon.lan] = (beacon.port, name, now)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get(
    "/v1/setup/beacon", summary="Stations waiting on my network",
    description="The Stations waiting for setup on the caller's own "
                "network (the same public address), newest first, each "
                "with the address of its setup page. Empty when there are "
                "none. Rate: 120 per 600 s (bucket `beacon_find`). Never "
                "cached.",
    operation_id="setup_beacon_get",
    responses=spec.ok({"stations": [{"url": "http://192.168.1.23:8654/",
                                     "name": "station", "seen_s": 12}]},
                      spec.R429),
    openapi_extra=spec.MAP_TIER,
)
def find(request: Request, response: Response):
    settings = request.app.state.settings
    ratelimit.throttle(request, 120, settings.rate_window_s,
                       bucket="beacon_find")
    response.headers["Cache-Control"] = "no-store"
    now = time.monotonic()
    _sweep(now)
    entries = _book.get(network_of(ratelimit.client_ip(request)), {})
    found = sorted(entries.items(), key=lambda kv: -kv[1][2])
    return {"stations": [
        {"url": "http://%s:%d/" % (lan, port), "name": name,
         "seen_s": round(now - seen)}
        for lan, (port, name, seen) in found]}
