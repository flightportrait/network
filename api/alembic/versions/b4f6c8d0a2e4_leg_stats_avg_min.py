"""average block time on route-leg statistics

Revision ID: b4f6c8d0a2e4
Revises: a1b9c7d2e5f8
Create Date: 2026-08-29
"""
import sqlalchemy as sa

from alembic import op

revision = "b4f6c8d0a2e4"
down_revision = "a1b9c7d2e5f8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ref_leg_stats",
                  sa.Column("avg_min", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("ref_leg_stats", "avg_min")
