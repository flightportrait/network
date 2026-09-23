"""live_state: small process state kept across API restarts

Additive: one new table; the downgrade drops it.

Revision ID: b5d7f9a1c3e4
Revises: a3c5e7f9b1d2
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "b5d7f9a1c3e4"
down_revision = "a3c5e7f9b1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_state",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("live_state")
