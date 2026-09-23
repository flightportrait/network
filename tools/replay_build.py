#!/usr/bin/env python3
"""Build the map's playback files from readsb's heatmap history.

readsb (`--heatmap=15 --write-globe-history=DIR`) keeps a snapshot of
the whole sky every 15 s, in half-hour files:

    DIR/YYYY/MM/DD/heatmap/NN.bin.ttf      NN = 00..47, gzip

Each file is a list of 16-byte records (little-endian): a header of
slice offsets, then for every 15 s slice a marker (0x0e7f7c9d, the
slice's time in ms split over the next two words, the step in ms)
followed by one record per aircraft: address (top bits: address type),
lat and lon x 1e6, altitude in 25 ft steps (-123 on the ground) and
ground speed x 10. A record whose lat has bit 30 set carries the
aircraft's callsign (8 ASCII bytes over lon/alt/gs) and squawk instead.

That layout is readsb's own; the map reads ours instead, so an upgrade
never breaks an archived day. For every day this writes:

    OUT/replay/v1/days.json                 the days on offer
    OUT/replay/v1/YYYY-MM-DD/index.json     chunk names, traffic by 5 min
    OUT/replay/v1/YYYY-MM-DD/HHMM.json      one per half hour (gzip)
    OUT/replay/v1/YYYY-MM-DD/tracks/XX.json each aircraft's whole day,
                                            sharded by the address's last
                                            two hex digits (gzip)

A chunk is {"v":1, "t0": unix start, "step": 15, "n": slices,
"ac": [[hex, callsign, registration, type, squawk, [k, lat, lon, alt,
gs, ...]], ...]} with lat/lon x 1e5, altitude in feet (GROUND for the
ground) and speed in knots; k is the slice index. Headings are left to
the map, which reads them off successive positions. A track shard is
{"v":1, "day", "t0": the day's first second, "step": 15, "ac": {hex:
[registration, type, [[s, callsign], ...], [s, lat, lon, alt, gs, ...]]}}
with s the 15 s slice of the day and the callsign list marking where
the callsign changes (an aircraft can fly several legs a day): what a
selected flight needs, in one small file. Chunk and track files are
gzip bytes under a .json name: serve them with Content-Encoding: gzip.

    python3 tools/replay_build.py GLOBE_DIR OUT [--db aircraft.csv.gz]
                                   [--from 2026-09-10] [--to 2026-09-22]
                                   [--partial]

--db is readsb's aircraft database (hex;reg;type;...), for types and
registrations. Days still being written are skipped unless --partial.
"""
import argparse
import datetime as dt
import gzip
import json
import os
import struct
import sys

MARK = 0x0E7F7C9D
GROUND = -99999
ONE_DAY = dt.timedelta(days=1)


def load_db(path):
    db = {}
    if not path:
        return db
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            p = line.rstrip("\n").split(";")
            if len(p) >= 3 and (p[1] or p[2]):
                db[p[0].lower()] = (p[1], p[2])
    return db


def read_heatmap(path):
    """Yield (slice_time_ms, step_ms, [records]) per slice."""
    b = gzip.open(path).read()
    n = len(b) // 16
    rec = struct.Struct("<Iiihh")
    rows = [rec.unpack_from(b, i * 16) for i in range(n)]
    cur = None
    for r in rows:
        if r[0] == MARK:
            if cur:
                yield cur
            t_ms = (r[1] & 0xFFFFFFFF) * 4294967296 + (r[2] & 0xFFFFFFFF)
            step = (r[3] & 0xFFFF) | ((r[4] & 0xFFFF) << 16)
            cur = (t_ms, step, [])
        elif cur is not None:
            cur[2].append(r)
    if cur:
        yield cur


def build_chunk(path, db):
    ac = {}
    t0 = step = None
    slices = list(read_heatmap(path))
    if not slices:
        return None, []
    t0 = slices[0][0] / 1000.0
    step = (slices[0][1] or 15000) / 1000.0
    counts = []
    for k, (t_ms, _, recs) in enumerate(slices):
        seen = set()
        for addr, lat, lon, alt, gs in recs:
            hexs = "%06x" % (addr & 0xFFFFFF)
            a = ac.get(hexs)
            if a is None:
                a = ac[hexs] = {"f": "", "sq": "", "p": {}}
            if lat & (1 << 30):
                raw = struct.pack("<ihh", lon, alt, gs)
                cs = raw.split(b"\0")[0].decode("ascii", "replace").strip()
                if cs:
                    a["f"] = cs
                sq = lat & 0xFFFF
                if sq:
                    a["sq"] = "%04d" % sq
                continue
            if alt == -123:
                ft = GROUND
            else:
                ft = alt * 25
            a["p"][k] = (round(lat / 10.0), round(lon / 10.0), ft,
                         round(gs / 10.0))
            seen.add(hexs)
        counts.append(len(seen))
    out = []
    for hexs in sorted(ac):
        a = ac[hexs]
        if not a["p"]:
            continue
        reg, typ = db.get(hexs, ("", ""))
        flat = []
        for k in sorted(a["p"]):
            flat.append(k)
            flat.extend(a["p"][k])
        out.append([hexs, a["f"], reg, typ, a["sq"], flat])
    chunk = {"v": 1, "t0": int(t0), "step": step, "n": len(slices),
             "ac": out}
    return chunk, counts


def write_gz_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = json.dumps(obj, separators=(",", ":")).encode()
    with open(path + ".part", "wb") as fh:
        fh.write(gzip.compress(data, 9, mtime=0))
    os.replace(path + ".part", path)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    os.replace(path + ".part", path)


def build_day(globe, out, day, db, partial):
    src = os.path.join(globe, day.strftime("%Y/%m/%d"), "heatmap")
    if not os.path.isdir(src):
        return None
    files = sorted(f for f in os.listdir(src) if f.endswith(".bin.ttf"))
    if len(files) < 48 and not partial:
        return None
    dest = os.path.join(out, "replay", "v1", day.isoformat())
    chunks, buckets = [], [0] * 288
    day0 = int(dt.datetime(day.year, day.month, day.day,
                           tzinfo=dt.timezone.utc).timestamp())
    tracks = {}
    for f in files:
        nn = int(f.split(".")[0])
        name = "%02d%02d" % (nn // 2, (nn % 2) * 30)
        chunk, counts = build_chunk(os.path.join(src, f), db)
        if not chunk:
            continue
        write_gz_json(os.path.join(dest, name + ".json"), chunk)
        chunks.append(name)
        base = int(round((chunk["t0"] - day0) / chunk["step"]))
        for hexs, cs, reg, typ, _sq, flat in chunk["ac"]:
            tr = tracks.get(hexs)
            if tr is None:
                tr = tracks[hexs] = [reg, typ, [], []]
            if cs and (not tr[2] or tr[2][-1][1] != cs):
                tr[2].append([base + flat[0], cs])
            for i in range(0, len(flat), 5):
                tr[3].append(base + flat[i])
                tr[3].extend(flat[i + 1:i + 5])
        # traffic for the timeline: the busiest slice of every 5 minutes
        for k, c in enumerate(counts):
            t = chunk["t0"] + k * chunk["step"]
            b = int((t % 86400) // 300)
            if 0 <= b < 288:
                buckets[b] = max(buckets[b], c)
    shards = {}
    for hexs, tr in tracks.items():
        shards.setdefault(hexs[-2:], {})[hexs] = tr
    for key, ac in shards.items():
        write_gz_json(os.path.join(dest, "tracks", key + ".json"),
                      {"v": 1, "day": day.isoformat(), "t0": day0,
                       "step": 15, "ac": ac})
    write_json(os.path.join(dest, "index.json"),
               {"v": 1, "day": day.isoformat(), "step": 15,
                "chunks": chunks, "traffic": buckets, "tracks": True,
                "complete": len(chunks) == 48})
    return {"day": day.isoformat(), "chunks": len(chunks),
            "complete": len(chunks) == 48}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("globe")
    ap.add_argument("out")
    ap.add_argument("--db")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--partial", action="store_true")
    a = ap.parse_args()
    db = load_db(a.db)
    years = sorted(d for d in os.listdir(a.globe) if d.isdigit())
    days = []
    for y in years:
        for m in sorted(os.listdir(os.path.join(a.globe, y))):
            for d in sorted(os.listdir(os.path.join(a.globe, y, m))):
                try:
                    days.append(dt.date(int(y), int(m), int(d)))
                except ValueError:
                    pass
    if a.start:
        days = [d for d in days if d >= dt.date.fromisoformat(a.start)]
    if a.end:
        days = [d for d in days if d <= dt.date.fromisoformat(a.end)]
    built = []
    for day in days:
        r = build_day(a.globe, a.out, day, db, a.partial)
        if r:
            built.append(r)
            print("%s  %d chunks%s" % (r["day"], r["chunks"],
                  "" if r["complete"] else "  (partial)"), flush=True)
    # the list of days merges with what an earlier run already published
    listing = os.path.join(a.out, "replay", "v1", "days.json")
    have = {}
    if os.path.exists(listing):
        with open(listing) as fh:
            for d in json.load(fh).get("days", []):
                have[d["day"]] = d
    for r in built:
        have[r["day"]] = r
    write_json(listing, {"v": 1, "days": [have[k] for k in sorted(have)]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
