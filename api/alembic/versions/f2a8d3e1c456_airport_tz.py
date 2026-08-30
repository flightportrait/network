"""ref_airports.tz — Olson timezone for local departure times

Revision ID: f2a8d3e1c456
Revises: e7c4a1f9b023
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

revision = "f2a8d3e1c456"
down_revision = "e7c4a1f9b023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ref_airports",
                  sa.Column("tz", sa.String(40), nullable=True))


def downgrade() -> None:
    op.drop_column("ref_airports", "tz")
