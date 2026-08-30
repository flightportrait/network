"""Per-IP, per-bucket rate limiting.

Process-local sliding window: good enough for a single-process service on a
single box, zero infrastructure. Correctness assumes ONE uvicorn worker;
`--workers N` would silently multiply every
limit by N. Every route throttles before doing any other work, each with
its own bucket so one hot endpoint cannot starve the rest. Deliberately
self-contained — this stack imports nothing from the FlightPortrait server
(that boundary is the point of network/).
"""
import math
import time
from collections import defaultdict, deque

from fastapi import Request

from .errors import ApiError

_windows = defaultdict(lambda: defaultdict(deque))
_calls_since_sweep = 0
_SWEEP_EVERY = 4096


def client_ip(request: Request) -> str:
    """The caller's IP, optionally read from a trusted proxy header.

    Behind a trusted reverse proxy the socket peer is the proxy, so
    NETWORK_API_CLIENT_IP_HEADER set to the proxy's real-client header
    (e.g. cf-connecting-ip) restores the real client. Never configure a
    header on a directly-exposed deployment — clients could then choose
    their own identity.
    """
    header = request.app.state.settings.client_ip_header
    if header:
        value = request.headers.get(header, "")
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _sweep(now: float, window_s: int) -> None:
    """Drop idle IPs so the maps never grow with client cardinality."""
    for bucket in _windows.values():
        dead = [ip for ip, window in bucket.items()
                if not window or window[-1] <= now - window_s]
        for ip in dead:
            del bucket[ip]


def throttle(request: Request, limit: int, window_s: int, bucket: str) -> None:
    global _calls_since_sweep
    ip = client_ip(request)
    now = time.monotonic()
    _calls_since_sweep += 1
    if _calls_since_sweep >= _SWEEP_EVERY:
        _calls_since_sweep = 0
        _sweep(now, window_s)
    window = _windows[bucket][ip]
    while window and window[0] <= now - window_s:
        window.popleft()
    if len(window) >= limit:
        retry_s = max(1, math.ceil(window[0] + window_s - now)) if window \
            else window_s
        raise ApiError(
            429, "rate_limited", "slow down",
            headers={
                "Retry-After": str(retry_s),
                "RateLimit-Limit": str(limit),
                "RateLimit-Remaining": "0",
                "RateLimit-Reset": str(retry_s),
            },
        )
    window.append(now)


def reset() -> None:
    """Tests only: rate state is process-global."""
    _windows.clear()
