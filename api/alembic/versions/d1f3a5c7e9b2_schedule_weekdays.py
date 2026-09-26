"""ref_schedule.weekdays: the days a flight keeps another slot

Additive: one nullable column; the downgrade drops it.

Revision ID: d1f3a5c7e9b2
Revises: c9e1a3b5d7f0
Create Date: 2026-09-26
"""
from alembic import op
import sqlalchemy as sa

revision = "d1f3a5c7e9b2"
down_revision = "c9e1a3b5d7f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ref_schedule",
                  sa.Column("weekdays", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("ref_schedule", "weekdays")
