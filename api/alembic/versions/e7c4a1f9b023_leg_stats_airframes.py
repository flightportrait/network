"""ref_leg_stats.airframes — the tails that fly each route

Revision ID: e7c4a1f9b023
Revises: d5b2e9a3c710
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

revision = "e7c4a1f9b023"
down_revision = "d5b2e9a3c710"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ref_leg_stats",
                  sa.Column("airframes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("ref_leg_stats", "airframes")
