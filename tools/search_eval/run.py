#!/usr/bin/env python3
"""The golden set for /v1/search: what a person types, and what they
expect to see first. Runs every query against a server and reports
pass/fail per tag, an overall score, and the known gaps apart (queries
the search does not answer yet; they fail without failing the run).

    python3 tools/search_eval/run.py                      # local networkd
    python3 tools/search_eval/run.py --base https://data.flightportrait.com
    python3 tools/search_eval/run.py --record out.json    # keep the answers
    python3 tools/search_eval/run.py --replay out.json    # score them again

Queries go out as typed and redirects are followed, so the canonical
form (/v1/search answers any other spelling with a redirect to it) is
part of what is checked. Against anything but a local address the
requests are at least a second apart: the public API is rate-limited
(600 per 600 s for search) and shared.

Each line of golden.jsonl:
    {"q": "SQ 322", "expect_top": [{"kind": "flight", "id": "SIA322"}],
     "expect_in_top_k": [...], "expect_not_in_top_k": [...], "k": 5,
     "tags": ["flight", "space"], "known_gap": false, "note": "..."}
A matcher holds any of kind, id, detail_prefix, label_contains; all it
holds must match. expect_top[i] must match the i-th result;
expect_in_top_k each some result among the first k (default 5);
expect_not_in_top_k none of them.

Exit status: 0 when every entry not marked known_gap passes, 1 when
any fails, 2 on a request error. Standard library only.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "flightportrait-network search_eval (https://flightportrait.com)"
LOCAL = ("127.0.0.1", "localhost", "::1")
META = "_recorded"   # where --record keeps the base URL and the time


def load(path):
    entries = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            e = json.loads(line)
            if "q" not in e:
                raise SystemExit("%s:%d: no q" % (path, n))
            entries.append(e)
    return entries


def fetch(base, q, timeout=30):
    url = base.rstrip("/") + "/v1/search?" + urllib.parse.urlencode({"q": q})
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return {"status": r.status, "url": r.geturl(),
                        "body": json.loads(r.read().decode("utf-8"))}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(float(e.headers.get("Retry-After") or 30))
                continue
            try:
                body = json.loads(e.read().decode("utf-8"))
            except ValueError:
                body = None
            return {"status": e.code, "url": url, "body": body}
    raise RuntimeError("unreachable")


def matches(m, r):
    if "kind" in m and r.get("kind") != m["kind"]:
        return False
    if "id" in m and r.get("id") != m["id"]:
        return False
    if "detail_prefix" in m and not (r.get("detail") or "").startswith(
            m["detail_prefix"]):
        return False
    if "label_contains" in m and m["label_contains"].lower() \
            not in (r.get("label") or "").lower():
        return False
    return True


def judge(e, answer):
    """The reasons an entry fails (empty: it passes)."""
    if answer["status"] != 200 or not isinstance(answer["body"], dict):
        return ["status %s" % answer["status"]]
    results = answer["body"].get("results") or []
    k = e.get("k", 5)
    top = results[:k]
    why = []
    for i, m in enumerate(e.get("expect_top", [])):
        if i >= len(results) or not matches(m, results[i]):
            why.append("#%d not %s" % (i + 1, show(m)))
    for m in e.get("expect_in_top_k", []):
        if not any(matches(m, r) for r in top):
            why.append("%s not in top %d" % (show(m), k))
    for m in e.get("expect_not_in_top_k", []):
        if any(matches(m, r) for r in top):
            why.append("%s in top %d" % (show(m), k))
    return why


def show(m):
    return "/".join(str(m[f]) for f in ("kind", "id", "detail_prefix",
                                        "label_contains") if f in m)


def got(answer, n=3):
    results = (answer.get("body") or {}).get("results") or []
    return ", ".join("%s/%s" % (r["kind"], r["id"]) for r in results[:n]) \
        or "nothing"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8092")
    ap.add_argument("--golden", default=os.path.join(HERE, "golden.jsonl"))
    ap.add_argument("--pace", type=float, default=None,
                    help="seconds between requests (at least 1 off-box)")
    ap.add_argument("--record", help="keep the answers here (a query "
                    "already in the file is not asked again)")
    ap.add_argument("--replay", help="score answers kept by --record")
    ap.add_argument("--tag", action="append", help="only these tags")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    entries = load(args.golden)
    if args.tag:
        entries = [e for e in entries if set(e.get("tags", [])) & set(args.tag)]
    host = urllib.parse.urlparse(args.base).hostname or ""
    pace = args.pace if args.pace is not None else (
        0.0 if host in LOCAL else 1.1)
    if host not in LOCAL:
        pace = max(pace, 1.0)

    kept = {}
    source = args.replay or args.record
    if source and os.path.exists(source):
        with open(source, encoding="utf-8") as fh:
            kept = json.load(fh)
    answers = {}
    asked = 0
    for e in entries:
        q = e["q"]
        if q in kept:
            answers[q] = kept[q]
            continue
        if args.replay:
            print("no kept answer for %r" % q, file=sys.stderr)
            return 2
        if asked and pace:
            time.sleep(pace)
        try:
            answers[q] = kept[q] = fetch(args.base, q)
        except (urllib.error.URLError, OSError, ValueError) as err:
            print("request failed for %r: %s" % (q, err), file=sys.stderr)
            return 2
        asked += 1
    if args.record:
        if asked:
            kept[META] = {"base": args.base,
                          "at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
        with open(args.record, "w", encoding="utf-8") as fh:
            json.dump(kept, fh, ensure_ascii=False, indent=1, sort_keys=True)

    by_tag, failures, gaps = {}, [], []
    passed = counted = 0
    for e in entries:
        why = judge(e, answers[e["q"]])
        gap = bool(e.get("known_gap"))
        if gap:
            gaps.append((e, why))
        else:
            counted += 1
            passed += not why
            if why:
                failures.append((e, why))
        for t in e.get("tags", []) or ["untagged"]:
            s = by_tag.setdefault(t, [0, 0, 0, 0])  # pass, total, gap-pass, gaps
            if gap:
                s[2] += not why
                s[3] += 1
            else:
                s[0] += not why
                s[1] += 1
        if args.verbose:
            print("%s %-36r %s" % ("gap " if gap else ("ok  " if not why else
                                                      "FAIL"),
                                    e["q"], got(answers[e["q"]])))

    meta = kept.get(META) if (args.replay or not asked) else None
    meta = meta or {"base": args.base,
                    "at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
    print("search eval: %s, %d queries (%s%s)" % (
        meta["base"], len(entries), meta["at"],
        ", replayed" if args.replay else ""))
    print()
    print("%-14s %9s  %s" % ("tag", "pass", "known gaps (passing)"))
    for t in sorted(by_tag):
        p, n, gp, g = by_tag[t]
        print("%-14s %4d/%-4d  %s" % (t, p, n, ("%d (%d)" % (g, gp)) if g else ""))
    print()
    score = passed / counted if counted else 1.0
    print("overall: %d/%d = %.1f%% (known gaps excluded)" % (
        passed, counted, 100 * score))
    if failures:
        print()
        print("failing:")
        for e, why in failures:
            print("  %-34r %s; got %s" % (e["q"], "; ".join(why),
                                           got(answers[e["q"]])))
    if gaps:
        print()
        print("known gaps (%d, %d now passing):" % (
            len(gaps), sum(1 for _, w in gaps if not w)))
        for e, why in gaps:
            state = "passes now" if not why else "; ".join(why)
            note = (" [" + e["note"] + "]") if e.get("note") else ""
            print("  %-34r %s; got %s%s" % (e["q"], state,
                                             got(answers[e["q"]]), note))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
