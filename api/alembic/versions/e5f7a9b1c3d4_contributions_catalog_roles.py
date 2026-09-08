"""Airport roles, community contributions, the route catalog.

ref_airports.role says what a field is for (commercial, general,
military, closed). contributions holds community answers to published
gaps as submitted, with the checks run against observation.
route_catalog holds the approved, dated answers the service may serve
when observation has none.

Revision ID: e5f7a9b1c3d4
Revises: d2e4f6a8b0c1
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa

revision = "e5f7a9b1c3d4"
down_revision = "d2e4f6a8b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ref_airports",
                  sa.Column("role", sa.String(12), nullable=True))
    op.create_table(
        "contributions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("callsign", sa.String(12), nullable=False),
        sa.Column("origin", sa.String(4), nullable=True),
        sa.Column("dest", sa.String(4), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("note", sa.String(280), nullable=True),
        sa.Column("contact", sa.String(120), nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(280), nullable=True),
    )
    op.create_index("ix_contributions_callsign", "contributions", ["callsign"])
    op.create_index("ix_contributions_status", "contributions", ["status"])
    op.create_table(
        "route_catalog",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("callsign", sa.String(12), nullable=False),
        sa.Column("origin", sa.String(4), nullable=False),
        sa.Column("dest", sa.String(4), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("source", sa.String(12), nullable=False),
        sa.Column("contribution_id", sa.Integer(),
                  sa.ForeignKey("contributions.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_reason", sa.String(20), nullable=True),
    )
    op.create_index("ix_route_catalog_callsign", "route_catalog", ["callsign"])


def downgrade() -> None:
    op.drop_table("route_catalog")
    op.drop_table("contributions")
    op.drop_column("ref_airports", "role")
