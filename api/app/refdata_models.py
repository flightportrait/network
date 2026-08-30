"""Reference data: the aircraft registry, airline/type/airport references
and the derived views.

Everything here is open data with per-row provenance. The merge policy
lives in the schema's `source` column plus SOURCE_RANK, not in route
code, so no future endpoint can launder a low-trust claim over a better
one — same doctrine as the stations privacy design in models.py.

`ref_airframes.source` ranks: an import at equal or
higher rank overwrites non-null fields; a lower rank only fills nulls.
Provenance also makes any source droppable wholesale if its license
posture sours: DELETE WHERE source = X, re-derive, done.
"""
import datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .models import PKBigInt, utcnow

SOURCE_RANK = {"fp-dump": 0, "tar1090": 1, "registry": 2, "override": 3}


class RefAirframe(Base):
    __tablename__ = "ref_airframes"

    # 24-bit ICAO hex, lowercase — matches what readsb emits.
    hex: Mapped[str] = mapped_column(String(6), primary_key=True)
    registration: Mapped[str | None] = mapped_column(String(16),
                                                     nullable=True)
    type_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Free-text owner/operator as the source states it, plus an uppercased
    # copy so fleet lookups can hit an index instead of upper()-scanning.
    operator_name: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True)
    operator_norm: Mapped[str | None] = mapped_column(String(120),
                                                      nullable=True,
                                                      index=True)
    # Observed operator: the airframe's majority callsign prefix from the
    # route evidence. Registries barely carry airline operators
    # outside the US (verified against tar1090-db 2026-08-28: zero
    # "Singapore Airlines" rows) — what we *watched* the airframe fly as
    # is the stronger claim, and it's ours.
    operator_icao: Mapped[str | None] = mapped_column(String(3),
                                                      nullable=True,
                                                      index=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Raw source flags (e.g. tar1090-db's dbFlags digits), stored verbatim
    # and never interpreted — we don't assert what we haven't verified.
    flags: Mapped[str | None] = mapped_column(String(8), nullable=True)

    source: Mapped[str] = mapped_column(String(16), nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)


class RefAirline(Base):
    __tablename__ = "ref_airlines"

    icao: Mapped[str] = mapped_column(String(3), primary_key=True)
    iata: Mapped[str | None] = mapped_column(String(2), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Sourced brand hexes, primary first — brand facts, not artwork.
    palette: Mapped[list | None] = mapped_column(JSON, nullable=True)


class RefAlliance(Base):
    """A global airline alliance from a dated official roster snapshot."""
    __tablename__ = "ref_alliances"

    slug: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(48), nullable=False)
    website_url: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(String(255), nullable=False)
    source_checked_at: Mapped[datetime.date] = mapped_column(Date,
                                                              nullable=False)
    # Populated only when an official mark is cleared for public display.
    # The public API and frontend work without it.
    logo_asset_url: Mapped[str | None] = mapped_column(String(255),
                                                       nullable=True)


class RefAllianceMembership(Base):
    """One sourced relationship between an operating airline and alliance.

    `member` is an alliance member in its own right. `group-brand` is an
    operating brand covered through a member group (for example Hawaiian
    through Alaska Air Group). `affiliate` is reserved for an explicitly
    named affiliate, never inferred from a codeshare or contract operation.
    """
    __tablename__ = "ref_alliance_memberships"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True,
                                    autoincrement=True)
    alliance_slug: Mapped[str] = mapped_column(
        String(32), ForeignKey("ref_alliances.slug", ondelete="CASCADE"),
        nullable=False, index=True)
    airline_icao: Mapped[str] = mapped_column(
        String(3), ForeignKey("ref_airlines.icao"), nullable=False,
        index=True)
    relationship: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, index=True)
    sponsor_icao: Mapped[str | None] = mapped_column(
        String(3), ForeignKey("ref_airlines.icao"), nullable=True)
    effective_from: Mapped[datetime.date | None] = mapped_column(Date,
                                                                  nullable=True)
    effective_to: Mapped[datetime.date | None] = mapped_column(Date,
                                                                nullable=True)
    source_url: Mapped[str] = mapped_column(String(255), nullable=False)
    source_checked_at: Mapped[datetime.date] = mapped_column(Date,
                                                              nullable=False)
    note: Mapped[str | None] = mapped_column(String(240), nullable=True)


class RefType(Base):
    __tablename__ = "ref_types"

    designator: Mapped[str] = mapped_column(String(4), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    category: Mapped[str | None] = mapped_column(String(12), nullable=True)


class RefAirport(Base):
    __tablename__ = "ref_airports"

    ident: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    iso_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    municipality: Mapped[str | None] = mapped_column(String(80),
                                                     nullable=True)
    iata: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # Olson timezone (e.g. "Asia/Dubai"), for local departure times.
    # Sourced from OpenFlights (refdata_ingest airport_tz).
    tz: Mapped[str | None] = mapped_column(String(40), nullable=True)


class RefRoute(Base):
    __tablename__ = "ref_routes"

    callsign: Mapped[str] = mapped_column(String(12), primary_key=True)
    # [origin, ...via, dest] — already confidence-gated at export;
    # nothing below the gate reaches us.
    chain: Mapped[list] = mapped_column(JSON, nullable=False)
    # Prefix resolved against ref_airlines at derive time; null when the
    # callsign doesn't look like an airline flight.
    airline_icao: Mapped[str | None] = mapped_column(String(3),
                                                     nullable=True,
                                                     index=True)


class RefAirlineCountry(Base):
    """Derived, recomputed by `refdata_ingest derive`, never hand-edited:
    countries an airline's observed routes touch, for the map highlight."""
    __tablename__ = "ref_airline_countries"

    airline_icao: Mapped[str] = mapped_column(String(3), primary_key=True)
    iso_country: Mapped[str] = mapped_column(String(2), primary_key=True)
    n_routes: Mapped[int] = mapped_column(Integer, nullable=False)


class RefLegStat(Base):
    """Per-airline route-leg statistics, aggregated from the
    legs.db flight-log artifact: for each airline
    and undirected airport pair, how many flights the network observed
    over the window, on how many distinct days, and which aircraft types
    flew it. This is the schedule/frequency + aircraft-per-leg layer over
    the plain ref_routes chains — recomputed wholesale by `refdata_ingest
    leg_stats`, never hand-edited."""
    __tablename__ = "ref_leg_stats"

    airline_icao: Mapped[str] = mapped_column(String(3), primary_key=True)
    o: Mapped[str] = mapped_column(String(4), primary_key=True)
    d: Mapped[str] = mapped_column(String(4), primary_key=True)
    n_flights: Mapped[int] = mapped_column(Integer, nullable=False)
    n_days: Mapped[int] = mapped_column(Integer, nullable=False)
    # Flights per week over the observation window — an observed lower
    # bound (our antennas miss flights), presented as such, not as a
    # published schedule.
    per_week: Mapped[float] = mapped_column(Float, nullable=False)
    # Mean gate-to-gate minutes over legs with both timestamps, bounded
    # to plausible durations at derive time; null when none qualify.
    avg_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # [[type, count], ...] most-flown first, top few — the aircraft that
    # usually flies this leg.
    types: Mapped[list] = mapped_column(JSON, nullable=True)
    # [[hex, reg, count], ...] most-frequent first, top few — the actual
    # airframes (tails) the network watched fly this leg.
    airframes: Mapped[list] = mapped_column(JSON, nullable=True)


class RefSchedule(Base):
    """The inferred fixed schedule: one row per flight number and leg,
    derived from legs.db. Because scheduled
    departures cluster tightly, the typical departure — converted to the
    origin airport's LOCAL time first, so daylight saving does not split
    one flight into two slots — is the schedule. Directional: EY22
    outbound and EY23 return are separate rows. Recomputed wholesale by
    `refdata_ingest schedule`."""
    __tablename__ = "ref_schedule"

    callsign: Mapped[str] = mapped_column(String(12), primary_key=True)
    org: Mapped[str] = mapped_column(String(4), primary_key=True)
    dst: Mapped[str] = mapped_column(String(4), primary_key=True)
    airline_icao: Mapped[str] = mapped_column(String(3), index=True)
    # Local minute-of-day at each end (0..1439); null if the tz is unknown.
    dep_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    arr_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Dominant aircraft type on this flight number.
    type_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # The marketed flight number (SK907) when a published board row was
    # matched to this service — the callsign column stays the ATC truth.
    flight: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # observed | published | both — where this row's knowledge comes from.
    source: Mapped[str] = mapped_column(String(12), nullable=False,
                                       default="observed",
                                       server_default="observed")
    # How many times the network observed it — the confidence weight.
    n_flights: Mapped[int] = mapped_column(Integer, nullable=False)


class RefImport(Base):
    """One row per ingest run — the audit trail the reference-data design promises."""
    __tablename__ = "ref_imports"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True,
                                    autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)
