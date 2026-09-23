#!/usr/bin/env python3
"""Keep a day's North Atlantic crossings from the adsb.lol archive.

adsb.lol publishes each day's traces (ODbL) as a release of split tar
parts. This streams one release straight from GitHub, nothing but the
kept traces touching disk, and keeps the flights that crossed the North
Atlantic that day: cruising both east of 15W and west of 50W, between
40N and 70N. Their ocean gap is what tools/nat_backtest.py scores.

    python3 tools/nat_flights.py 2026-09-23 OUT_DIR [--parts N]

A day is ~3.6 GB streamed; expect several minutes on a fast link.
"""
import argparse
import gzip
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.request

UA = "flightportrait-network/nat-flights (+https://flightportrait.com/network/)"
SUFFIXES = ["aa", "ab", "ac", "ad", "ae"]
EAST_LON, WEST_LON = -15.0, -50.0
MIN_ALT = 25000


def release_for(day):
    return "v%s-planes-readsb-prod-0" % day.replace("-", ".")


class Parts(io.RawIOBase):
    """Sequential read across a release's .tar.aa/.ab/... parts; a
    missing later part means the archive already ended."""

    def __init__(self, day):
        rel = release_for(day)
        base = ("https://github.com/adsblol/globe_history_%s/releases/download"
                % day[:4])
        self.urls = ["%s/%s/%s.tar.%s" % (base, rel, rel, s) for s in SUFFIXES]
        self.resp, self.started = None, False

    def readable(self):
        return True

    def readinto(self, b):
        while True:
            if self.resp is None:
                if not self.urls:
                    return 0
                url = self.urls.pop(0)
                try:
                    self.resp = urllib.request.urlopen(
                        urllib.request.Request(url, headers={"User-Agent": UA}),
                        timeout=120)
                except urllib.error.HTTPError as e:
                    if e.code == 404 and self.started:
                        return 0
                    raise
                print("streaming", url, flush=True)
                self.started = True
            n = self.resp.readinto(b)
            if n:
                return n
            self.resp = None


def crossed(trace):
    east = west = False
    for p in trace.get("trace", []):
        lat, lon, alt = p[1], p[2], p[3]
        if lat is None or lon is None or not isinstance(alt, (int, float)):
            continue
        if alt < MIN_ALT or not (40 <= lat <= 70):
            continue
        east = east or lon > EAST_LON
        west = west or lon < WEST_LON
        if east and west:
            return True
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("day")
    ap.add_argument("out")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    seen = kept = 0
    stream = io.BufferedReader(Parts(args.day), buffer_size=1 << 20)
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for m in tar:
            if not m.isfile() or "trace_full_" not in m.name:
                continue
            raw = tar.extractfile(m).read()
            seen += 1
            try:
                trace = json.loads(gzip.decompress(raw))
            except OSError:
                try:
                    trace = json.loads(raw)
                except ValueError:
                    continue
            except ValueError:
                continue
            if crossed(trace):
                kept += 1
                with gzip.open(os.path.join(args.out, os.path.basename(m.name)
                                            .replace(".json", ".json.gz")), "wt") as fh:
                    json.dump(trace, fh, separators=(",", ":"))
            if seen % 100000 == 0:
                print("%d traces read, %d crossings" % (seen, kept), flush=True)
    print("done: %d traces read, %d North Atlantic crossings kept" % (seen, kept))


if __name__ == "__main__":
    sys.exit(main())
