"""ref_schedule — the inferred fixed departure schedule

Revision ID: a1b9c7d2e5f8
Revises: f2a8d3e1c456
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b9c7d2e5f8"
down_revision = "f2a8d3e1c456"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ref_schedule",
        sa.Column("callsign", sa.String(12), primary_key=True),
        sa.Column("org", sa.String(4), primary_key=True),
        sa.Column("dst", sa.String(4), primary_key=True),
        sa.Column("airline_icao", sa.String(3), nullable=False),
        sa.Column("dep_min", sa.Integer(), nullable=True),
        sa.Column("arr_min", sa.Integer(), nullable=True),
        sa.Column("type_code", sa.String(8), nullable=True),
        sa.Column("n_flights", sa.Integer(), nullable=False),
    )
    op.create_index("ix_ref_schedule_airline", "ref_schedule",
                    ["airline_icao"])
    op.create_index("ix_ref_schedule_od", "ref_schedule", ["org", "dst"])


def downgrade() -> None:
    op.drop_table("ref_schedule")
