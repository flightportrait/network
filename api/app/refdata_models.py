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

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, \
    JSON, String, Text, UniqueConstraint
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
    # Raw source flags (e.g. tar1090-db's dbFlags digits), stored verbatim.
    # Character i is bit i ("10": military, "0001": LADD); networkd's
    # private fleet tier reads bit 0 (military), nothing public does.
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
    # What the field is for: commercial (scheduled service), general
    # (everything else with a runway), military, closed. Derived at
    # ingest from the registry's service flag and name; corrections
    # arrive as contributions.
    role: Mapped[str | None] = mapped_column(String(12), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    iso_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    municipality: Mapped[str | None] = mapped_column(String(80),
                                                     nullable=True)
    iata: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # IANA time zone (e.g. "Asia/Dubai"), for local departure times,
    # computed from the airport's coordinates (refdata_ingest airport_tz).
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
    # The weekdays this flight keeps another slot altogether (more than
    # WEEKDAY_APART_MIN from dep_min): {"4": [dep_min, arr_min]}, 0 =
    # Monday, local minutes as above; null when every day is the same.
    weekdays: Mapped[dict | None] = mapped_column(JSON(none_as_null=True),
                                                  nullable=True)
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


class Claim(Base):
    """One distinct answer to an open question: this callsign flies this
    route. Everyone who says so is an Endorsement beneath it; anonymous
    repeats with nothing to add only raise anonymous_count. Never served
    as fact: an approved claim is copied into RouteCatalog."""
    __tablename__ = "claims"
    __table_args__ = (UniqueConstraint("callsign", "origin", "dest",
                                       name="uq_claims_route"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    callsign: Mapped[str] = mapped_column(String(12), index=True)
    origin: Mapped[str] = mapped_column(String(4), nullable=False)
    dest: Mapped[str] = mapped_column(String(4), nullable=False)
    # pending | approved | rejected
    status: Mapped[str] = mapped_column(String(12), nullable=False,
                                       default="pending", index=True)
    # corroborated | contradicted | unverified | contested
    verdict: Mapped[str | None] = mapped_column(String(14), nullable=True)
    checks: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    anonymous_count: Mapped[int] = mapped_column(Integer, nullable=False,
                                                 default=0)
    first_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    last_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # operator | verdict
    reviewed_by: Mapped[str | None] = mapped_column(String(12), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(280),
                                                    nullable=True)


class Endorsement(Base):
    """A person behind a claim: the name they chose, their note, the key
    they sent if any. contact never existed here by design."""
    __tablename__ = "endorsements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"),
                                          index=True)
    handle: Mapped[str | None] = mapped_column(String(40), nullable=True,
                                               index=True)
    note: Mapped[str | None] = mapped_column(String(280), nullable=True)
    key_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    valid_from: Mapped[datetime.date | None] = mapped_column(
        Date, nullable=True)
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    # The edge submission this came from; one endorsement per submission.
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True,
                                                  unique=True)


class PullState(Base):
    """Where the pull left off: the last edge submission id filed."""
    __tablename__ = "pull_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pulled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class RouteCatalog(Base):
    """Routes the network could not observe end to end, answered by the
    community and approved by the operator. Dated: a schedule change is
    a new row that closes the old one, never an edit. Observation always
    outranks a catalog row when both speak."""
    __tablename__ = "route_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    callsign: Mapped[str] = mapped_column(String(12), index=True)
    origin: Mapped[str] = mapped_column(String(4), nullable=False)
    dest: Mapped[str] = mapped_column(String(4), nullable=False)
    # Stops between the ends when the callsign is a chain.
    via: Mapped[list | None] = mapped_column(JSON, nullable=True)
    valid_from: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    # Open (null) while current; the date it stopped being true otherwise.
    valid_to: Mapped[datetime.date | None] = mapped_column(Date,
                                                           nullable=True)
    source: Mapped[str] = mapped_column(String(12), nullable=False,
                                       default="community")
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("claims.id"),
                                                 nullable=True)
    approved_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    # Why a row closed: superseded, contradicted (by observation), withdrawn.
    closed_reason: Mapped[str | None] = mapped_column(String(20),
                                                      nullable=True)


# ---- the airframe lifetime record ------------------------------------------
# One row per physical aircraft; hexes, registrations and operators are
# dated spells pointing at it, because a hex follows the registration and
# an aircraft sold abroad gets a new one. Spells join one airframe only on
# a serial number from a named source, never on a guess: a missing merge
# leaves a history incomplete, a wrong one corrupts two.

class Airframe(Base):
    __tablename__ = "airframes"

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True,
                                    autoincrement=True)
    type_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Manufacturer serial number; null until a registry gives it. Not
    # unique, even per type: Dassault restarts serials per variant and
    # homebuilts reuse "1". Same-aircraft matching needs manufacturer,
    # model and serial together (AIRFRAMES.md D2).
    msn: Mapped[str | None] = mapped_column(String(32), nullable=True,
                                            index=True)
    manufacturer: Mapped[str | None] = mapped_column(String(80),
                                                     nullable=True)
    built_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_observed: Mapped[datetime.date | None] = mapped_column(
        Date, nullable=True)
    last_observed: Mapped[datetime.date | None] = mapped_column(
        Date, nullable=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)


class AirframeSpell(Base):
    """A dated stretch of one identity: kind hex, registration or
    operator (ICAO airline designator). last_date null = current per a
    registry; observed spells always carry both dates."""
    __tablename__ = "airframe_spells"
    __table_args__ = (Index("ix_airframe_spells_kind_value", "kind",
                            "value"),)

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True,
                                    autoincrement=True)
    airframe_id: Mapped[int] = mapped_column(
        PKBigInt, ForeignKey("airframes.id", ondelete="CASCADE"),
        nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False)
    value: Mapped[str] = mapped_column(String(16), nullable=False)
    first_date: Mapped[datetime.date | None] = mapped_column(Date,
                                                             nullable=True)
    last_date: Mapped[datetime.date | None] = mapped_column(Date,
                                                            nullable=True)
    # Observed legs behind the spell; null for registry spells.
    n_obs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)


class AirframeClaim(Base):
    """One source's statement of one field. Re-seeing the same statement
    moves last_seen; a changed value is a new row, so the history of what
    each source said stays."""
    __tablename__ = "airframe_claims"
    __table_args__ = (UniqueConstraint("airframe_id", "field", "source",
                                       "value", name="uq_airframe_claims"),)

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True,
                                    autoincrement=True)
    airframe_id: Mapped[int] = mapped_column(
        PKBigInt, ForeignKey("airframes.id", ondelete="CASCADE"),
        nullable=False, index=True)
    field: Mapped[str] = mapped_column(String(24), nullable=False)
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(200),
                                                   nullable=True)
    licence: Mapped[str] = mapped_column(String(24), nullable=False)
    first_seen: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)


class AirframeEvent(Base):
    """Something that happened to an airframe, worded as what we observed
    or what a named source states. visibility 'held' keeps a row out of
    every public response until reviewed (a 7500 squawk)."""
    __tablename__ = "airframe_events"
    __table_args__ = (UniqueConstraint("airframe_id", "kind", "at", "source",
                                       name="uq_airframe_events"),)

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True,
                                    autoincrement=True)
    airframe_id: Mapped[int] = mapped_column(
        PKBigInt, ForeignKey("airframes.id", ondelete="CASCADE"),
        nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True),
                                                  nullable=False, index=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_ref: Mapped[str | None] = mapped_column(String(200),
                                                     nullable=True)
    visibility: Mapped[str] = mapped_column(String(8), nullable=False,
                                            default="public",
                                            server_default="public")


class EstimateScore(Base):
    """One night's measured accuracy of the position estimator: the
    summary app.estimate_score records for a UTC day of traces."""
    __tablename__ = "estimate_scores"

    day: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)


class LiveState(Base):
    """Small process state kept across API restarts (the estimate book's
    memory): one JSON value per key, overwritten in place."""
    __tablename__ = "live_state"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[list | dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)


class NatMessage(Base):
    """One North Atlantic track message as published (app.nat): the
    day's westbound (Shanwick) or eastbound (Gander) set of tracks, kept
    so past days can be scored and replayed."""
    __tablename__ = "nat_messages"
    __table_args__ = (UniqueConstraint("issuer", "valid_from",
                                       name="uq_nat_messages"),)

    id: Mapped[int] = mapped_column(PKBigInt, primary_key=True,
                                    autoincrement=True)
    issuer: Mapped[str] = mapped_column(String(8), nullable=False)
    tmi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_from: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True)
    valid_to: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    tracks: Mapped[list] = mapped_column(JSON, nullable=False)
    # the message as published, so a better parser can re-read old days
    raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow)
