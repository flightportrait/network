"""estimate_scores: the nightly measured accuracy of estimated positions

Additive: one new table; the downgrade drops it.

Revision ID: a3c5e7f9b1d2
Revises: f4b6d8e0a2c4
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "a3c5e7f9b1d2"
down_revision = "f4b6d8e0a2c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estimate_scores",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("estimate_scores")
