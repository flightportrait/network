"""Catalog rows keep the stops between the ends.

Revision ID: c3e5a7b9d1f3
Revises: a7b9c1d3e5f7
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa

revision = "c3e5a7b9d1f3"
down_revision = "a7b9c1d3e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("route_catalog", sa.Column("via", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("route_catalog", "via")
