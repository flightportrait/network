#!/usr/bin/env python3
"""Mirror NASA's Black Marble city lights, the map's night layer.

The live map shows city lights where it is night. They come from NASA's
VIIRS Black Marble (2016 composite, public domain, served by NASA GIBS
as web-mercator tiles). GIBS is slow and marks every tile uncacheable,
so the map serves its own copy; this fetches the untouched source tiles
once, into DIR/{z}/{x}/{y}.png. Zoom 8 is the product's full resolution
(about 500 m a pixel); there is nothing finer to fetch.

Resumable: tiles already on disk are skipped, so an interrupted run
picks up where it stopped. Failures are retried with backoff and listed
at the end; run it again to fill them.

build turns that archive into the tiles the map serves: every pixel
darker than the map's light threshold (NIGHT_FS in web/index.html
ignores anything under 16 % luminance) goes to black, the lights stay
exactly as they are, and the result is lossless WebP. On screen nothing
changes; the terrain and sensor noise that made up most of each file
are gone, about seven times smaller. Needs Pillow.

    python3 tools/night_lights.py fetch RAW [--zoom 0-8] [--workers 12]
    python3 tools/night_lights.py build RAW OUT [--zoom 0-8]
"""
import argparse
import concurrent.futures
import os
import sys
import time
import urllib.request

SRC = ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
       "VIIRS_Black_Marble/default/2016-01-01/"
       "GoogleMapsCompatible_Level8/{z}/{y}/{x}.png")
UA = "flightportrait-network/night-lights (+https://flightportrait.com/network/)"


def fetch(z, x, y, out):
    path = os.path.join(out, str(z), str(x), "%d.png" % y)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return "skip", path
    url = SRC.format(z=z, x=x, y=y)
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            if not body.startswith(b"\x89PNG"):
                raise ValueError("not a PNG")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".part"
            with open(tmp, "wb") as fh:
                fh.write(body)
            os.replace(tmp, path)
            return "ok", path
        except Exception as e:  # noqa: BLE001 - retried, then reported
            err = e
            time.sleep(2 ** attempt)
    return "fail", "%s (%s)" % (url, err)


# luminance below which the map's night shader draws no light (0.16)
THRESHOLD = 41


def build_one(src, dst):
    from PIL import Image
    im = Image.open(src).convert("RGB")
    # "L" is ITU-R 601 luma, the weights the shader uses
    mask = im.convert("L").point(lambda v: 255 if v >= THRESHOLD else 0)
    out = Image.composite(im, Image.new("RGB", im.size), mask)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    out.save(dst + ".part", format="WEBP", lossless=True, method=6)
    os.replace(dst + ".part", dst)
    return os.path.getsize(src), os.path.getsize(dst)


def build(raw, out, zooms):
    jobs = []
    for z in zooms:
        for x in range(2 ** z):
            for y in range(2 ** z):
                src = os.path.join(raw, str(z), str(x), "%d.png" % y)
                dst = os.path.join(out, str(z), str(x), "%d.webp" % y)
                if not os.path.exists(src):
                    print("missing", src, file=sys.stderr)
                    return 1
                jobs.append((src, dst))
    before = after = 0
    with concurrent.futures.ProcessPoolExecutor() as pool:
        for n, (a, b) in enumerate(
                pool.map(build_one, *zip(*jobs), chunksize=64), 1):
            before += a
            after += b
            if n % 5000 == 0 or n == len(jobs):
                print("%d/%d  %.0f MB -> %.0f MB" % (
                    n, len(jobs), before / 1e6, after / 1e6), flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("dir")
    f.add_argument("--zoom", default="0-8")
    f.add_argument("--workers", type=int, default=12)
    b = sub.add_parser("build")
    b.add_argument("raw")
    b.add_argument("out")
    b.add_argument("--zoom", default="0-8")
    a = ap.parse_args()
    lo, _, hi = a.zoom.partition("-")
    zooms = range(int(lo), int(hi or lo) + 1)
    if a.cmd == "build":
        return build(a.raw, a.out, zooms)
    jobs = [(z, x, y) for z in zooms
            for x in range(2 ** z) for y in range(2 ** z)]
    done = {"ok": 0, "skip": 0, "fail": 0}
    fails = []
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(a.workers) as pool:
        futs = [pool.submit(fetch, z, x, y, a.dir) for z, x, y in jobs]
        for n, f in enumerate(concurrent.futures.as_completed(futs), 1):
            kind, what = f.result()
            done[kind] += 1
            if kind == "fail":
                fails.append(what)
            if n % 500 == 0 or n == len(jobs):
                rate = (done["ok"] or 1) / max(time.time() - t0, 1)
                left = (len(jobs) - n) / rate if done["ok"] else 0
                print("%d/%d  new %d  skipped %d  failed %d  ~%d min left"
                      % (n, len(jobs), done["ok"], done["skip"],
                         done["fail"], left / 60), flush=True)
    for f in fails:
        print("FAILED", f, file=sys.stderr)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
