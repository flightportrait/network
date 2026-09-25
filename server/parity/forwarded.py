"""Python-only routes, straight vs through networkd's fallback."""
import sys, urllib.request
DIRECT, VIA = sys.argv[1], sys.argv[2]
HEADERS = ("content-type", "cache-control", "retry-after", "access-control-allow-origin",
           "access-control-allow-methods", "vary", "allow", "location")
CASES = [
    ("GET", "/openapi.json", {}, None), ("GET", "/docs", {}, None),
    ("GET", "/v1/estimated", {}, None), ("GET", "/v1/search?q=SIN", {}, None),
    ("GET", "/v1/stations", {}, None), ("GET", "/v1/airlines/SIA", {}, None),
    ("GET", "/v1/routes?callsign=SIA1", {}, None), ("GET", "/v1/gaps?limit=2", {}, None),
    ("GET", "/v1/search?q=SIN", {"Origin": "https://example.org"}, None),
    ("OPTIONS", "/v1/search", {"Origin": "https://example.org", "Access-Control-Request-Method": "GET"}, None),
    ("GET", "/nope/deeper?x=1", {}, None),
    ("POST", "/v1/setup/beacon", {"Content-Type": "application/json"}, b'{"x":1}'),
    ("GET", "/v1/setup/beacon", {}, None),
]
def fetch(base, m, p, h, body):
    req = urllib.request.Request(base + p, method=m, headers=h, data=body)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
bad = 0
for m, p, h, body in CASES:
    a, b = fetch(DIRECT, m, p, h, body), fetch(VIA, m, p, h, body)
    probs = []
    if a[0] != b[0]: probs.append("status %s vs %s" % (a[0], b[0]))
    for k in HEADERS:
        if a[1].get(k) != b[1].get(k): probs.append("header %s: %r vs %r" % (k, a[1].get(k), b[1].get(k)))
    if a[2] != b[2]: probs.append("body %d vs %d bytes: %r | %r" % (len(a[2]), len(b[2]), a[2][:120], b[2][:120]))
    bad += bool(probs)
    print(("ok   " if not probs else "DIFF ") + m, p, a[0], len(a[2]))
    for x in probs: print("     " + x)
print("%d of %d differ" % (bad, len(CASES)))
