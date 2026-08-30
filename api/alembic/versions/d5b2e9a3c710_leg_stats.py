"""per-airline route-leg statistics

Revision ID: d5b2e9a3c710
Revises: c3d8f5a1e6b2
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

revision = "d5b2e9a3c710"
down_revision = "c3d8f5a1e6b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ref_leg_stats",
        sa.Column("airline_icao", sa.String(3), primary_key=True),
        sa.Column("o", sa.String(4), primary_key=True),
        sa.Column("d", sa.String(4), primary_key=True),
        sa.Column("n_flights", sa.Integer(), nullable=False),
        sa.Column("n_days", sa.Integer(), nullable=False),
        sa.Column("per_week", sa.Float(), nullable=False),
        sa.Column("types", sa.JSON(), nullable=True),
    )
    op.create_index("ix_ref_leg_stats_airline", "ref_leg_stats",
                    ["airline_icao"])


def downgrade() -> None:
    op.drop_table("ref_leg_stats")
