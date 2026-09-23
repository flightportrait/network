"""Score the North Atlantic track method against a day's real crossings.

For each flight that crossed the North Atlantic (from the adsb.lol
archive, ODbL), the ocean gap (lost on one side, heard again on the
other) is scored at the moment it reappeared, twice: the current
estimator and the track method (fly the matched published track, then
on to the destination; the current estimator where no track matches).
The track messages are the ones the API kept that day (nat_messages).
Run in the nightly on yesterday; the result is recorded beside the
day's estimate score, so the decision to use the tracks rests on it.

    python -m app.nat_score [--day 2026-09-23] [--keep DIR]

tools/nat_flights.py and tools/nat_backtest.py run the same code on
files.
"""
import argparse
import datetime
import gzip
import io
import json
import os
import statistics
import sys
import tarfile
import urllib.error
import urllib.request

from . import estimate as E
from . import estimate_score as S
from . import nat

UA = "flightportrait-network/nat-score (+https://flightportrait.com/network/)"
SUFFIXES = ["aa", "ab", "ac", "ad", "ae"]
EAST_LON, WEST_LON = -15.0, -50.0
MIN_ALT = 25000
MIN_GAP_S = 1200


# ---- the day's crossings, streamed from the adsb.lol archive ----------

def release_for(day):
    return "v%s-planes-readsb-prod-0" % day.isoformat().replace("-", ".")


class Parts(io.RawIOBase):
    """Sequential read across a release's .tar.aa/.ab/... parts; a
    missing later part means the archive already ended."""

    def __init__(self, day):
        rel = release_for(day)
        base = ("https://github.com/adsblol/globe_history_%d/releases/download"
                % day.year)
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


def crossings(day):
    """Yield the day's crossing traces, streamed."""
    stream = io.BufferedReader(Parts(day), buffer_size=1 << 20)
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for m in tar:
            if not m.isfile() or "trace_full_" not in m.name:
                continue
            raw = tar.extractfile(m).read()
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
                yield m.name, trace


# ---- scoring -----------------------------------------------------------

def ocean_gaps(pts):
    for a, b in zip(pts, pts[1:]):
        if b[0] - a[0] < MIN_GAP_S:
            continue
        if not all(isinstance(p[3], (int, float)) and p[3] >= 25000 and
                   40 <= p[1] <= 70 for p in (a, b)):
            continue
        if abs(a[2] - b[2]) < 5 or max(a[2], b[2]) > 5 or min(a[2], b[2]) < -75:
            continue
        yield a, b


def score(traces, messages, route_of, airports):
    rows = []
    counts = {"crossings": 0, "gaps": 0, "routed": 0, "in_track_hours": 0,
              "matched": 0}
    for trace in traces:
        counts["crossings"] += 1
        for a, b in ocean_gaps(list(S.points(trace))):
            counts["gaps"] += 1
            obs = {"lat": a[1], "lon": a[2], "alt": a[3], "gs": a[4] or 0,
                   "track": a[5]}
            if obs["track"] is None or not obs["gs"]:
                continue
            dest = E.pick_destination(a[1], a[2], a[5],
                                      route_of(a[6]) if a[6] else None, airports)
            if not dest:
                continue
            counts["routed"] += 1
            dt = b[0] - a[0]
            when = datetime.datetime.fromtimestamp(a[0], datetime.timezone.utc)
            tracks = nat.active(messages, when)
            if tracks:
                counts["in_track_hours"] += 1
            cla, clo, _, _ = E.project(obs, (dest[1], dest[2]), dt)
            cur = E.haversine_km(cla, clo, b[1], b[2])
            m = E.match_nat(obs, tracks) if tracks else None
            if m:
                counts["matched"] += 1
                nla, nlo, _, _ = E.project_path(
                    obs, [tuple(p) for p in m] + [(dest[1], dest[2])], dt)
                trk = E.haversine_km(nla, nlo, b[1], b[2])
            else:
                trk = cur
            rows.append({"cs": a[6], "gap_min": round(dt / 60), "matched": bool(m),
                         "current_km": round(cur, 1), "track_km": round(trk, 1),
                         "dir": "W" if b[2] < a[2] else "E"})
    return counts, rows


def summary(counts, rows):
    def med(xs):
        return round(statistics.median(xs), 1) if xs else None
    matched = [r for r in rows if r["matched"]]
    return {"counts": counts,
            "all": {"n": len(rows),
                    "current_median_km": med([r["current_km"] for r in rows]),
                    "track_median_km": med([r["track_km"] for r in rows])},
            "matched": {"n": len(matched),
                        "current_median_km": med([r["current_km"] for r in matched]),
                        "track_median_km": med([r["track_km"] for r in matched]),
                        "track_better": sum(1 for r in matched
                                            if r["track_km"] < r["current_km"])},
            "worst_matched": sorted(matched, key=lambda r: r["track_km"] -
                                    r["current_km"])[-5:]}


def messages_for(sessionmaker, day):
    """The track messages valid at any time of the UTC day."""
    from sqlalchemy import select
    from .refdata_models import NatMessage
    start = datetime.datetime.combine(day, datetime.time(), datetime.timezone.utc)
    end = start + datetime.timedelta(days=1)
    with sessionmaker() as session:
        rows = session.execute(select(NatMessage).where(
            NatMessage.valid_from < end, NatMessage.valid_to > start)).scalars().all()
        return [{"issuer": r.issuer, "tmi": r.tmi, "tracks": r.tracks,
                 "valid_from": r.valid_from.astimezone(datetime.timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "valid_to": r.valid_to.astimezone(datetime.timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ")} for r in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", help="UTC day, default yesterday")
    ap.add_argument("--keep", help="also write the crossings here (gzip JSON)")
    ap.add_argument("--no-record", action="store_true")
    args = ap.parse_args(argv)
    day = (datetime.date.fromisoformat(args.day) if args.day else
           datetime.datetime.now(datetime.timezone.utc).date()
           - datetime.timedelta(days=1))
    from .main import create_network_api_app
    from .routes_live import airport_coords, route_of
    from .settings import Settings
    app = create_network_api_app(Settings(), start_pollers=False)
    messages = messages_for(app.state.sessionmaker, day)
    if not messages:
        print("nat_score: no track messages kept for %s, nothing to score" % day,
              file=sys.stderr)
        return 1

    def traces():
        for name, trace in crossings(day):
            if args.keep:
                os.makedirs(args.keep, exist_ok=True)
                with gzip.open(os.path.join(args.keep, os.path.basename(name)
                                            .replace(".json", ".json.gz")), "wt") as fh:
                    json.dump(trace, fh, separators=(",", ":"))
            yield trace

    counts, rows = score(traces(), messages, lambda cs: route_of(app, cs),
                         airport_coords(app))
    doc = summary(counts, rows)
    doc["messages"] = [{"issuer": m["issuer"], "tmi": m["tmi"],
                        "valid_from": m["valid_from"]} for m in messages]
    print(json.dumps({"day": day.isoformat(), **doc}))
    if not args.no_record:
        from .refdata_models import EstimateScore
        with app.state.sessionmaker() as session:
            row = session.get(EstimateScore, day)
            if row is None:
                session.add(EstimateScore(day=day, detail={"nat": doc}))
            else:
                row.detail = dict(row.detail or {}, nat=doc)
            session.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
