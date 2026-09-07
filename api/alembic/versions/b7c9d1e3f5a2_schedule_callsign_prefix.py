"""ref_schedule callsign prefix index for /v1/search (text_pattern_ops
so LIKE 'SIA3%' walks it on Postgres).

Revision ID: b7c9d1e3f5a2
Revises: a1b2c3d4e5f6
Create Date: 2026-09-07
"""
from alembic import op

revision = "b7c9d1e3f5a2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE INDEX ix_ref_schedule_callsign_prefix"
                   " ON ref_schedule (callsign text_pattern_ops)")
    else:
        op.execute("CREATE INDEX ix_ref_schedule_callsign_prefix"
                   " ON ref_schedule (callsign)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_ref_schedule_callsign_prefix")
