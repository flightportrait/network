"""Reference routes: Python over Postgres vs networkd over the snapshot."""
import sys, time, urllib.request
PY, RS = sys.argv[1], sys.argv[2]
PATHS = sys.argv[3:]
def get(base, p):
    t = time.time()
    try:
        with urllib.request.urlopen(base + p, timeout=60) as r:
            return r.status, r.headers.get("cache-control"), r.read(), time.time() - t
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("cache-control"), e.read(), time.time() - t
bad = 0
for p in PATHS:
    a = get(PY, p); b = get(RS, p); b2 = get(RS, p)
    same = a[:3] == b[:3] == b2[:3]
    bad += not same
    print("%s %-34s %3d %8d B  python %6.1f ms  networkd %6.1f ms, again %5.2f ms" % (
        "ok  " if same else "DIFF", p, a[0], len(a[2]), a[3]*1000, b[3]*1000, b2[3]*1000))
    if not same:
        i = next((k for k in range(min(len(a[2]), len(b[2]))) if a[2][k] != b[2][k]), None)
        print("     status %s/%s cache %s/%s at byte %s\n     py %r\n     rs %r" % (a[0], b[0], a[1], b[1], i,
              a[2][max(0,(i or 0)-80):(i or 0)+80], b[2][max(0,(i or 0)-80):(i or 0)+80]))
print("%d of %d differ" % (bad, len(PATHS)))
