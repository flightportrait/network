"""Bucket isolation, window expiry, and header trust rules."""
from app import ratelimit


def test_buckets_isolated(ctx):
    client, app, sm, settings, readsb = ctx
    settings.now_rate_limit = 1
    settings.aircraft_rate_limit = 5
    ratelimit.reset()
    assert client.get("/v1/now").status_code == 200
    assert client.get("/v1/now").status_code == 429
    # A hot /v1/now must not starve /v1/aircraft:
    assert client.get("/v1/aircraft").status_code == 200


def test_429_is_no_store_with_retry_headers(ctx):
    client, app, sm, settings, readsb = ctx
    settings.now_rate_limit = 1
    ratelimit.reset()
    assert client.get("/v1/now").status_code == 200
    response = client.get("/v1/now")
    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": "rate_limited", "detail": "slow down"}
    retry = int(response.headers["retry-after"])
    assert 1 <= retry <= settings.rate_window_s
    assert response.headers["ratelimit-limit"] == "1"
    assert response.headers["ratelimit-remaining"] == "0"
    assert response.headers["ratelimit-reset"] == str(retry)


def test_header_trusted_only_when_configured(ctx):
    client, app, sm, settings, readsb = ctx
    settings.now_rate_limit = 1
    # Header NOT configured: spoofed identities share the socket-peer bucket.
    settings.client_ip_header = ""
    ratelimit.reset()
    assert client.get("/v1/now",
                      headers={"cf-connecting-ip": "1.1.1.1"}).status_code == 200
    assert client.get("/v1/now",
                      headers={"cf-connecting-ip": "2.2.2.2"}).status_code == 429
    # Header configured (tunnel deployment): distinct clients get distinct
    # buckets.
    settings.client_ip_header = "cf-connecting-ip"
    ratelimit.reset()
    assert client.get("/v1/now",
                      headers={"cf-connecting-ip": "1.1.1.1"}).status_code == 200
    assert client.get("/v1/now",
                      headers={"cf-connecting-ip": "2.2.2.2"}).status_code == 200
