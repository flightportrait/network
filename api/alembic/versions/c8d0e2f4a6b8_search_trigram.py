"""Near-miss search: pg_trgm and GIN trigram indexes on airport and
airline names, for "Chnagi" and "Heathro". Postgres only; other
dialects skip the near-miss path.

Revision ID: c8d0e2f4a6b8
Revises: b7c9d1e3f5a2
Create Date: 2026-09-07
"""
from alembic import op

revision = "c8d0e2f4a6b8"
down_revision = "b7c9d1e3f5a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX ix_ref_airports_name_trgm ON ref_airports"
               " USING gin (upper(name) gin_trgm_ops)")
    op.execute("CREATE INDEX ix_ref_airports_city_trgm ON ref_airports"
               " USING gin (upper(municipality) gin_trgm_ops)")
    op.execute("CREATE INDEX ix_ref_airlines_name_trgm ON ref_airlines"
               " USING gin (upper(name) gin_trgm_ops)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX ix_ref_airlines_name_trgm")
    op.execute("DROP INDEX ix_ref_airports_city_trgm")
    op.execute("DROP INDEX ix_ref_airports_name_trgm")
