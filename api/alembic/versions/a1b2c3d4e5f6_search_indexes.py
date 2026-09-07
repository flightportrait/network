"""ref_airframes prefix indexes for /v1/search: registration, and
registration without dashes. text_pattern_ops so LIKE 'A7-AN%' walks
the index on Postgres; plain indexes elsewhere.

Revision ID: a1b2c3d4e5f6
Revises: d9f4a6b8c2e1
Create Date: 2026-09-07
"""
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "d9f4a6b8c2e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE INDEX ix_ref_airframes_reg_prefix ON ref_airframes"
                   " (registration text_pattern_ops)")
        op.execute("CREATE INDEX ix_ref_airframes_reg_bare ON ref_airframes"
                   " (replace(registration, '-', '') text_pattern_ops)")
    else:
        op.execute("CREATE INDEX ix_ref_airframes_reg_prefix ON ref_airframes"
                   " (registration)")
        op.execute("CREATE INDEX ix_ref_airframes_reg_bare ON ref_airframes"
                   " (replace(registration, '-', ''))")


def downgrade() -> None:
    op.execute("DROP INDEX ix_ref_airframes_reg_bare")
    op.execute("DROP INDEX ix_ref_airframes_reg_prefix")
