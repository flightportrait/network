"""Registry logic: identity, presence, sessions, and the privacy floor.

Pure functions over an injected SQLAlchemy session — unit-testable on
SQLite, no FastAPI imports.
"""
import datetime
import hashlib
import hmac

from sqlalchemy import select

from .models import Station, StationSession, utcnow


def _aware(dt: datetime.datetime) -> datetime.datetime:
    """SQLite hands back naive datetimes even for timezone=True columns;
    every stored timestamp in this service is UTC, so pin it."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def normalize_uuid(uuid_str: str) -> str | None:
    """Canonical form: 32 lowercase hex chars, dashes stripped.

    Returns None for anything that is not a UUID — the caller turns that
    into the same 404 an unknown UUID gets (no format oracle).
    """
    if not isinstance(uuid_str, str):
        return None
    cleaned = uuid_str.strip().lower().replace("-", "")
    if len(cleaned) != 32:
        return None
    try:
        int(cleaned, 16)
    except ValueError:
        return None
    return cleaned


def is_anonymous(uuid_str: str) -> bool:
    """readsb invents a half-filled UUID (xxxx...-0000-000000000000) for
    connections that did not send one — internal plumbing like the
    mlathub loop and upstream forwards. They are not stations."""
    normalized = normalize_uuid(uuid_str)
    return normalized is None or normalized.endswith("0" * 16)


def hash_uuid(uuid_str: str) -> str | None:
    normalized = normalize_uuid(uuid_str)
    if normalized is None:
        return None
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def half_id_of(uuid_str: str) -> str | None:
    normalized = normalize_uuid(uuid_str)
    return normalized[:16] if normalized else None


def public_id_of(uuid_sha256: str) -> str:
    return "fp-" + uuid_sha256[:10]


def parse_clients(raw: dict) -> list[dict]:
    """clients.json rows -> presence rows. The host:port column (the
    feeder's IP) is discarded HERE, at the boundary — it never enters the
    registry or any response."""
    rows = []
    for entry in raw.get("clients") or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 9:
            continue
        uuid_str = entry[0]
        if is_anonymous(uuid_str):
            continue
        rows.append({
            "uuid": uuid_str,
            "kbit_s": float(entry[2]),
            "conn_time_s": float(entry[3]),
            "msgs_per_s": float(entry[4]),
            "positions_per_s": float(entry[5]),
            "rtt_ms": float(entry[7]),
            "positions_total": int(entry[8]),
        })
    return rows


def upsert_presence(session, rows: list[dict],
                    now: datetime.datetime | None = None) -> dict:
    """Apply one clients.json poll: create/update stations, maintain
    connection sessions, close sessions for stations that vanished.
    Returns {half_id: live_row} for the in-memory presence map."""
    now = now or utcnow()
    presence: dict[str, dict] = {}
    seen_ids: set[int] = set()

    for row in rows:
        digest = hash_uuid(row["uuid"])
        half_id = half_id_of(row["uuid"])
        if digest is None or half_id is None:
            continue
        station = session.execute(
            select(Station).where(Station.uuid_sha256 == digest)
        ).scalar_one_or_none()
        if station is None:
            station = Station(
                public_id=public_id_of(digest),
                uuid_sha256=digest,
                half_id=half_id,
                first_seen=now,
                last_seen=now,
                positions_total=0,
            )
            session.add(station)
            session.flush()
        station.last_seen = now
        station.msgs_per_s = row["msgs_per_s"]
        station.positions_per_s = row["positions_per_s"]
        station.kbit_s = row["kbit_s"]
        station.rtt_ms = row["rtt_ms"]
        station.positions_total = max(station.positions_total,
                                      row["positions_total"])
        seen_ids.add(station.id)

        started_at = now - datetime.timedelta(seconds=row["conn_time_s"])
        open_session = session.execute(
            select(StationSession)
            .where(StationSession.station_id == station.id,
                   StationSession.ended_at.is_(None))
            .order_by(StationSession.started_at.desc())
        ).scalars().first()
        # conn_time resetting below the open session's start means the TCP
        # connection dropped and came back between polls: close and reopen.
        if open_session is not None and \
                started_at > _aware(open_session.started_at) + datetime.timedelta(seconds=30):
            open_session.ended_at = open_session.last_seen_at
            open_session = None
        if open_session is None:
            open_session = StationSession(
                station_id=station.id,
                started_at=started_at,
                last_seen_at=now,
                peak_msgs_per_s=0.0,
                positions_total=0,
            )
            session.add(open_session)
        open_session.last_seen_at = now
        open_session.peak_msgs_per_s = max(open_session.peak_msgs_per_s,
                                           row["msgs_per_s"])
        open_session.positions_total = max(open_session.positions_total,
                                           row["positions_total"])

        # Live stats only — the full UUID never enters the long-lived
        # presence map (it is the self-view credential; keep it out of
        # process state, same as it is kept out of the database).
        live = {k: v for k, v in row.items() if k != "uuid"}
        live["connected_since"] = started_at.isoformat()
        presence[half_id] = live

    # Stations with an open session that were absent from this poll have
    # disconnected: close at the last moment we actually saw them.
    for orphan in session.execute(
        select(StationSession).where(StationSession.ended_at.is_(None))
    ).scalars():
        if orphan.station_id not in seen_ids:
            orphan.ended_at = orphan.last_seen_at

    session.commit()
    return presence


def apply_receivers(session, raw: dict, coarse_decimals: int) -> None:
    """receivers.json -> coarse station locations. The midpoint of the
    traffic-derived coverage extent, rounded — computed by us, never typed
    by the feeder. Rows flagged badExtent are ignored."""
    for entry in raw.get("receivers") or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 10:
            continue
        half_id = (str(entry[0]).strip().lower().replace("-", ""))[:16]
        bad_extent = entry[7]
        if bad_extent:
            continue
        lat_avg, lon_avg = entry[8], entry[9]
        if lat_avg is None or lon_avg is None:
            continue
        station = session.execute(
            select(Station).where(Station.half_id == half_id)
        ).scalar_one_or_none()
        if station is None:
            continue
        station.coarse_lat = round(float(lat_avg), coarse_decimals)
        station.coarse_lon = round(float(lon_avg), coarse_decimals)
    session.commit()


def prune_sessions(session, retention_days: int,
                   now: datetime.datetime | None = None) -> int:
    now = now or utcnow()
    cutoff = now - datetime.timedelta(days=retention_days)
    stale = session.execute(
        select(StationSession).where(StationSession.ended_at.is_not(None),
                                     StationSession.ended_at < cutoff)
    ).scalars().all()
    for row in stale:
        session.delete(row)
    session.commit()
    return len(stale)


def lookup_by_uuid(session, uuid_str: str) -> Station | None:
    """Capability check: knowing the full UUID is the credential. One code
    path for malformed and unknown values so the route's 404 is uniform."""
    digest = hash_uuid(uuid_str)
    if digest is None:
        return None
    station = session.execute(
        select(Station).where(Station.uuid_sha256 == digest)
    ).scalar_one_or_none()
    if station is None:
        return None
    if not hmac.compare_digest(station.uuid_sha256, digest):
        return None
    return station


def online(station: Station, offline_after_s: int,
           now: datetime.datetime | None = None) -> bool:
    now = now or utcnow()
    return (now - _aware(station.last_seen)).total_seconds() <= offline_after_s


def serialize_public(station: Station, offline_after_s: int) -> dict:
    """Roster entry. Coarse coordinates only; no uuid material beyond the
    already-committed public id; nothing else leaves the registry."""
    return {
        "id": station.public_id,
        "label": station.label,
        "coarse_lat": station.coarse_lat,
        "coarse_lon": station.coarse_lon,
        "first_seen": station.first_seen.isoformat(),
        "last_seen": station.last_seen.isoformat(),
        "online": online(station, offline_after_s),
    }


def serialize_self(station: Station, live: dict | None,
                   aircraft_seen: int | None,
                   offline_after_s: int) -> dict:
    recent = sorted(station.sessions, key=lambda s: s.started_at,
                    reverse=True)[:10]
    return {
        "id": station.public_id,
        "online": online(station, offline_after_s),
        "connected_since": (live or {}).get("connected_since"),
        "messages_per_s": (live or {}).get("msgs_per_s"),
        "positions_per_s": (live or {}).get("positions_per_s"),
        "kbit_s": (live or {}).get("kbit_s"),
        "rtt_ms": (live or {}).get("rtt_ms"),
        "positions_total": station.positions_total,
        "aircraft_seen": aircraft_seen,
        "first_seen": station.first_seen.isoformat(),
        "last_seen": station.last_seen.isoformat(),
        "recent_sessions": [
            {
                "started_at": s.started_at.isoformat(),
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "peak_messages_per_s": s.peak_msgs_per_s,
                "positions_total": s.positions_total,
            }
            for s in recent
        ],
    }
