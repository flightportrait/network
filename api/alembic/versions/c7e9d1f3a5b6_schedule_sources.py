"""published-board provenance on the schedule

Revision ID: c7e9d1f3a5b6
Revises: b4f6c8d0a2e4
Create Date: 2026-08-29
"""
import sqlalchemy as sa

from alembic import op

revision = "c7e9d1f3a5b6"
down_revision = "b4f6c8d0a2e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ref_schedule",
                  sa.Column("flight", sa.String(8), nullable=True))
    op.add_column("ref_schedule",
                  sa.Column("source", sa.String(12), nullable=False,
                            server_default="observed"))


def downgrade() -> None:
    op.drop_column("ref_schedule", "source")
    op.drop_column("ref_schedule", "flight")
