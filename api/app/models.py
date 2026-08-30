"""Stations registry.

The privacy design lives in this schema, not in route code, so it cannot
be bypassed by a future endpoint:

- The full station UUID is NEVER persisted. We store its sha256 (the
  capability verifier for /v1/stations/{uuid}) and the 16-hex half_id
  readsb uses in receivers.json and filter_uuid. A database leak therefore
  leaks no capability tokens.
- Feeder IP addresses are never persisted; clients.json rows are consumed
  and the host:port column discarded at the poller boundary.
- coarse_lat/lon come from readsb's traffic-derived coverage extents
  (receivers.json midpoints), rounded — never from anything the feeder
  typed in. Exact coordinates do not exist in this system.
"""
import datetime

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, \
    Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# SQLite (the test database) only autoincrements INTEGER primary keys.
PKBigInt = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Station(Base):
    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True,
                                    autoincrement=True)
    # "fp-" + sha256(uuid)[:10] — safe to show anywhere, reveals no UUID bits
    # beyond what the verifier hash already commits to.
    public_id: Mapped[str] = mapped_column(String(16), unique=True,
                                           nullable=False)
    uuid_sha256: Mapped[str] = mapped_column(String(64), unique=True,
                                             nullable=False)
    half_id: Mapped[str] = mapped_column(String(16), unique=True,
                                         nullable=False)

    first_seen: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    coarse_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    coarse_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Future named-metro override ("Singapore") — display only, operator-set.
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Last-observed stats, overwritten every poll; history is in sessions.
    msgs_per_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    positions_per_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    kbit_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    positions_total: Mapped[int] = mapped_column(BigInteger, nullable=False,
                                                 default=0)

    sessions: Mapped[list["StationSession"]] = relationship(
        back_populates="station", cascade="all, delete-orphan")


class StationSession(Base):
    __tablename__ = "station_sessions"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True,
                                    autoincrement=True)
    station_id: Mapped[int] = mapped_column(
        ForeignKey("stations.id", ondelete="CASCADE"), nullable=False,
        index=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    peak_msgs_per_s: Mapped[float] = mapped_column(Float, nullable=False,
                                                   default=0.0)
    positions_total: Mapped[int] = mapped_column(BigInteger, nullable=False,
                                                 default=0)

    station: Mapped[Station] = relationship(back_populates="sessions")


Index("ix_station_sessions_station_started",
      StationSession.station_id, StationSession.started_at.desc())
