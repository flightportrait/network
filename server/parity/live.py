"""Ask both servers the same questions; report every difference."""
import json, re, sys, urllib.request

PY, RS = sys.argv[1], sys.argv[2]
HEADERS = ("content-type", "cache-control", "retry-after", "ratelimit-limit",
           "ratelimit-remaining", "access-control-allow-origin", "access-control-allow-methods",
           "access-control-max-age", "access-control-allow-headers", "vary", "allow")
long_bbox = "1," * 45 + "1"
CASES = [
    ("GET", "/", {}), ("GET", "/healthz", {}), ("GET", "/v1/now", {}),
    ("GET", "/v1/aircraft", {}),
    ("GET", "/v1/aircraft?bbox=-10,35,30,60", {}),
    ("GET", "/v1/aircraft?bbox=170,-60,-170,60", {}),
    ("GET", "/v1/aircraft?bbox=", {}),
    ("GET", "/v1/aircraft?bbox=1,2,3", {}),
    ("GET", "/v1/aircraft?bbox=1,5,3,4", {}),
    ("GET", "/v1/aircraft?bbox=1,2,3,inf", {}),
    ("GET", "/v1/aircraft?bbox=%201,2,3,4", {}),
    ("GET", "/v1/aircraft?bbox=" + long_bbox, {}),
    ("GET", "/v2/point/51.47/-0.4543/100", {}),
    ("GET", "/v2/point/10/20/1", {}),
    ("GET", "/v2/point/45.5/179.9/400", {}),
    ("GET", "/v2/point/abc/1/1", {}),
    ("GET", "/v2/point/1/xyz/1", {}),
    ("GET", "/v2/point/95/1/1", {}),
    ("GET", "/v2/point/1/1/-5", {}),
    ("GET", "/v2/point/1/1/0", {}),
    ("GET", "/v1/trace/a00001", {}),
    ("GET", "/v1/trace/A00007", {}),
    ("GET", "/v1/trace/a00003", {}),
    ("GET", "/v1/trace/zzzzzz", {}),
    ("GET", "/v1/trace/a00005", {}),
    ("GET", "/nope", {}),
    ("POST", "/v1/now", {}),
    ("GET", "/v1/now", {"Origin": "https://example.org"}),
    ("OPTIONS", "/v1/aircraft", {"Origin": "https://example.org", "Access-Control-Request-Method": "GET"}),
    ("OPTIONS", "/v1/aircraft", {"Origin": "https://example.org", "Access-Control-Request-Method": "POST"}),
    ("OPTIONS", "/v1/aircraft", {"Origin": "https://example.org", "Access-Control-Request-Method": "GET",
                                 "Access-Control-Request-Headers": "X-Custom"}),
    ("GET", "/v1/stream", {}),
]

def fetch(base, method, path, headers):
    req = urllib.request.Request(base + path, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

def norm(path, body):
    if path.startswith("/v2/point/"):
        body = re.sub(rb'"ptime":[0-9.e+-]+', b'"ptime":0', body)
    return body

bad = 0
for method, path, headers in CASES:
    a, b = fetch(PY, method, path, headers), fetch(RS, method, path, headers)
    problems = []
    if a[0] != b[0]:
        problems.append("status %s vs %s" % (a[0], b[0]))
    for h in HEADERS:
        if a[1].get(h) != b[1].get(h):
            problems.append("header %s: %r vs %r" % (h, a[1].get(h), b[1].get(h)))
    ba, bb = norm(path, a[2]), norm(path, b[2])
    if ba != bb:
        i = next((k for k in range(min(len(ba), len(bb))) if ba[k] != bb[k]), min(len(ba), len(bb)))
        problems.append("body differs at byte %d (%d vs %d bytes):\n      py %r\n      rs %r"
                        % (i, len(ba), len(bb), ba[max(0, i - 60):i + 60], bb[max(0, i - 60):i + 60]))
    tag = "ok  " if not problems else "DIFF"
    bad += bool(problems)
    print("%s %s %s  (%d, %d bytes)" % (tag, method, path[:70], a[0], len(a[2])))
    for p in problems:
        print("     " + p)
print("\n%d of %d differ" % (bad, len(CASES)))
