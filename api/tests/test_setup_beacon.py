"""Find my station: a Station waiting for setup is found from the join
page on the same network, and from nowhere else."""
import pytest

from app import ratelimit
from app import setup_beacon as B


@pytest.fixture
def beacon(ctx, monkeypatch):
    client, app, sm, settings, readsb = ctx
    B.reset()
    ratelimit.reset()
    # the caller's public address, as the proxy header would carry it
    monkeypatch.setattr(ratelimit, "client_ip",
                        lambda request: request.headers.get("x-ip", "203.0.113.7"))
    yield client
    B.reset()


def test_found_from_the_same_network_only(beacon):
    r = beacon.post("/v1/setup/beacon", json={"lan": "192.168.1.23"},
                    headers={"x-ip": "203.0.113.7"})
    assert r.status_code == 204
    got = beacon.get("/v1/setup/beacon", headers={"x-ip": "203.0.113.7"}).json()
    assert [s["url"] for s in got["stations"]] == ["http://192.168.1.23:8654/"]
    assert got["stations"][0]["name"] == "station"
    # another home sees nothing
    other = beacon.get("/v1/setup/beacon", headers={"x-ip": "198.51.100.9"}).json()
    assert other == {"stations": []}


def test_ipv6_pairs_by_the_home_network(beacon):
    beacon.post("/v1/setup/beacon", json={"lan": "10.0.0.5", "name": "attic"},
                headers={"x-ip": "2001:db8:1:2::10"})
    # the phone: same /64, a different address of its own
    got = beacon.get("/v1/setup/beacon", headers={"x-ip": "2001:db8:1:2:abcd::99"}).json()
    assert got["stations"][0]["url"] == "http://10.0.0.5:8654/"
    assert got["stations"][0]["name"] == "attic"
    assert beacon.get("/v1/setup/beacon",
                      headers={"x-ip": "2001:db8:1:3::99"}).json() == {"stations": []}


@pytest.mark.parametrize("body", [
    {"lan": "8.8.8.8"},                    # not a home address
    {"lan": "127.0.0.1"},
    {"lan": "169.254.1.1"},
    {"lan": "fd00::1"},                    # IPv6: a phone cannot open it plainly
    {"lan": "192.168.1.2", "port": 0},
    {"lan": "192.168.1.2", "name": "<script>"},
    {"lan": "not an ip"},
])
def test_only_a_private_address_is_taken(beacon, body):
    assert beacon.post("/v1/setup/beacon", json=body).status_code == 422
    assert beacon.get("/v1/setup/beacon").json() == {"stations": []}


def test_forgotten_after_ten_minutes_and_bounded(beacon, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(B.time, "monotonic", lambda: now[0])
    for i in range(B.MAX_PER_NETWORK + 3):
        beacon.post("/v1/setup/beacon", json={"lan": "192.168.1.%d" % (i + 2)})
        now[0] += 1
    assert len(beacon.get("/v1/setup/beacon").json()["stations"]) == B.MAX_PER_NETWORK
    now[0] += B.BEACON_TTL_S + 1
    assert beacon.get("/v1/setup/beacon").json() == {"stations": []}
    assert B._book == {}
