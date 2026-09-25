"""Airports: equal, or equal but for the order of rows Python's database
leaves unordered (ties); anything else is a real difference."""
import json, sys, urllib.request
PY, RS = sys.argv[1], sys.argv[2]
def get(base, p):
    try:
        with urllib.request.urlopen(base + p, timeout=60) as r: return r.status, r.read()
    except urllib.error.HTTPError as e: return e.code, e.read()
real = 0
for code in sys.argv[3:]:
    p = "/v1/airports/" + code
    (sa, a), (sb, b) = get(PY, p), get(RS, p)
    if (sa, a) == (sb, b):
        print("same     ", code); continue
    if sa != sb:
        print("REAL     ", code, "status", sa, sb); real += 1; continue
    A, B = json.loads(a), json.loads(b)
    notes = []
    for k in A:
        if A[k] == B.get(k): continue
        x, y = A[k], B.get(k)
        if isinstance(x, list) and isinstance(y, list) and sorted(map(json.dumps, x)) == sorted(map(json.dumps, y)):
            notes.append("%s: tie order" % k)
        elif k in ("board", "arrivals") and isinstance(x, list) and len(x) == len(y) == 80:
            # truncated at the limit: rows with the boundary's flight count may be swapped
            key = "flights"
            lo = min(r[key] for r in x)
            if [r for r in x if r[key] > lo] and sorted(map(json.dumps, [r for r in x if r[key] > lo])) == sorted(map(json.dumps, [r for r in y if r[key] > lo])):
                notes.append("%s: tie at the 80-row limit (flights == %d)" % (k, lo))
            else:
                notes.append("REAL %s" % k)
        elif k == "airlines" and sorted(map(json.dumps, x)) == sorted(map(json.dumps, y)):
            notes.append("airlines: tie order")
        elif k == "airlines" and len(A.get("board") or []) == 80 and A["board"] != B["board"]:
            # summed from the 80 departures: the tied rows at the cut move it
            notes.append("airlines: follows the board's tie at the cut")
        else:
            notes.append("REAL %s: %s | %s" % (k, json.dumps(x)[:200], json.dumps(y)[:200]))
    if list(A) != list(B): notes.append("REAL key order")
    bad = any(n.startswith("REAL") for n in notes)
    real += bad
    print("REAL     " if bad else "ties only", code, "; ".join(notes))
print("%d real differences" % real)
