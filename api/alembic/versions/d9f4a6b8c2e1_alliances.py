"""global airline alliances and dated membership relationships

Revision ID: d9f4a6b8c2e1
Revises: c7e9d1f3a5b6
Create Date: 2026-08-29
"""
import sqlalchemy as sa

from alembic import op

revision = "d9f4a6b8c2e1"
down_revision = "c7e9d1f3a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ref_alliances",
        sa.Column("slug", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(48), nullable=False),
        sa.Column("website_url", sa.String(255), nullable=False),
        sa.Column("source_url", sa.String(255), nullable=False),
        sa.Column("source_checked_at", sa.Date(), nullable=False),
        sa.Column("logo_asset_url", sa.String(255), nullable=True),
    )
    op.create_table(
        "ref_alliance_memberships",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("alliance_slug", sa.String(32),
                  sa.ForeignKey("ref_alliances.slug", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("airline_icao", sa.String(3),
                  sa.ForeignKey("ref_airlines.icao"), nullable=False),
        sa.Column("relationship", sa.String(16), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("sponsor_icao", sa.String(3),
                  sa.ForeignKey("ref_airlines.icao"), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("source_url", sa.String(255), nullable=False),
        sa.Column("source_checked_at", sa.Date(), nullable=False),
        sa.Column("note", sa.String(240), nullable=True),
    )
    op.create_index("ix_ref_alliance_memberships_alliance",
                    "ref_alliance_memberships", ["alliance_slug"])
    op.create_index("ix_ref_alliance_memberships_airline",
                    "ref_alliance_memberships", ["airline_icao"])
    op.create_index("ix_ref_alliance_memberships_status",
                    "ref_alliance_memberships", ["status"])


def downgrade() -> None:
    op.drop_table("ref_alliance_memberships")
    op.drop_table("ref_alliances")
