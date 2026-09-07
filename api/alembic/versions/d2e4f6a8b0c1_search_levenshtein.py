"""Near-miss search by edit distance: fuzzystrmatch for levenshtein().
Postgres only.

Revision ID: d2e4f6a8b0c1
Revises: c8d0e2f4a6b8
Create Date: 2026-09-07
"""
from alembic import op

revision = "d2e4f6a8b0c1"
down_revision = "c8d0e2f4a6b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS fuzzystrmatch")


def downgrade() -> None:
    pass
