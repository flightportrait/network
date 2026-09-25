"""For each differing path: is it only the order of equal-ranked rows
(same content, lists re-ordered only among ties / dict key order), or real?"""
import json, sys, urllib.request
PY, RS = sys.argv[1], sys.argv[2]
def get(b, p):
    try:
        with urllib.request.urlopen(b + p, timeout=60) as r: return r.read()
    except urllib.error.HTTPError as e: return e.read()
def canon(x):
    if isinstance(x, dict): return {k: canon(v) for k, v in sorted(x.items())}
    if isinstance(x, list): return sorted((canon(v) for v in x), key=lambda v: json.dumps(v, sort_keys=True))
    return x
for p in sys.argv[3:]:
    a, b = get(PY, p), get(RS, p)
    if a == b: continue
    A, B = json.loads(a), json.loads(b)
    if canon(A) == canon(B):
        # same content: which lists/dicts differ only in order?
        where = [k for k in A if A[k] != B.get(k)]
        print("ORDER ", p, where)
    else:
        diffs = [k for k in set(A) | set(B) if canon(A.get(k)) != canon(B.get(k))]
        print("REAL  ", p, diffs, json.dumps({k: A.get(k) for k in diffs})[:300], "|", json.dumps({k: B.get(k) for k in diffs})[:300])
