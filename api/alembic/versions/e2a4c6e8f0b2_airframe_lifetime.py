"""airframes, spells, claims, events — the airframe lifetime record

Additive only: four new tables, nothing existing touched. The downgrade
drops exactly these (docs: AIRFRAMES.md D9).

Revision ID: e2a4c6e8f0b2
Revises: c3e5a7b9d1f3
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "e2a4c6e8f0b2"
down_revision = "c3e5a7b9d1f3"
branch_labels = None
depends_on = None

BIGID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "airframes",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("type_code", sa.String(8), nullable=True),
        sa.Column("msn", sa.String(32), nullable=True),
        sa.Column("manufacturer", sa.String(80), nullable=True),
        sa.Column("built_year", sa.Integer(), nullable=True),
        sa.Column("first_observed", sa.Date(), nullable=True),
        sa.Column("last_observed", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("type_code", "msn", name="uq_airframes_type_msn"),
    )
    op.create_index("ix_airframes_last_observed", "airframes",
                    ["last_observed"])
    op.create_table(
        "airframe_spells",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("airframe_id", BIGID,
                  sa.ForeignKey("airframes.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("value", sa.String(16), nullable=False),
        sa.Column("first_date", sa.Date(), nullable=True),
        sa.Column("last_date", sa.Date(), nullable=True),
        sa.Column("n_obs", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
    )
    op.create_index("ix_airframe_spells_airframe_id", "airframe_spells",
                    ["airframe_id"])
    op.create_index("ix_airframe_spells_kind_value", "airframe_spells",
                    ["kind", "value"])
    op.create_table(
        "airframe_claims",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("airframe_id", BIGID,
                  sa.ForeignKey("airframes.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("field", sa.String(24), nullable=False),
        sa.Column("value", sa.String(200), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.String(200), nullable=True),
        sa.Column("licence", sa.String(24), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("airframe_id", "field", "source", "value",
                            name="uq_airframe_claims"),
    )
    op.create_index("ix_airframe_claims_airframe_id", "airframe_claims",
                    ["airframe_id"])
    op.create_table(
        "airframe_events",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("airframe_id", BIGID,
                  sa.ForeignKey("airframes.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("evidence_ref", sa.String(200), nullable=True),
        sa.Column("visibility", sa.String(8), nullable=False,
                  server_default="public"),
        sa.UniqueConstraint("airframe_id", "kind", "at", "source",
                            name="uq_airframe_events"),
    )
    op.create_index("ix_airframe_events_airframe_id", "airframe_events",
                    ["airframe_id"])
    op.create_index("ix_airframe_events_at", "airframe_events", ["at"])


def downgrade() -> None:
    op.drop_table("airframe_events")
    op.drop_table("airframe_claims")
    op.drop_table("airframe_spells")
    op.drop_table("airframes")
