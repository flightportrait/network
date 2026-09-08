"""The name a contributor is credited under.

Revision ID: f6a8b0c2d4e6
Revises: e5f7a9b1c3d4
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa

revision = "f6a8b0c2d4e6"
down_revision = "e5f7a9b1c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contributions",
                  sa.Column("handle", sa.String(40), nullable=True))
    op.create_index("ix_contributions_handle", "contributions", ["handle"])


def downgrade() -> None:
    op.drop_index("ix_contributions_handle", table_name="contributions")
    op.drop_column("contributions", "handle")
