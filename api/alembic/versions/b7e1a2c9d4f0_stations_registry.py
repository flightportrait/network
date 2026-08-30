"""stations registry

Revision ID: b7e1a2c9d4f0
Revises:
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa

revision = "b7e1a2c9d4f0"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("public_id", sa.String(16), nullable=False, unique=True),
        sa.Column("uuid_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("half_id", sa.String(16), nullable=False, unique=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("coarse_lat", sa.Float(), nullable=True),
        sa.Column("coarse_lon", sa.Float(), nullable=True),
        sa.Column("label", sa.String(80), nullable=True),
        sa.Column("msgs_per_s", sa.Float(), nullable=True),
        sa.Column("positions_per_s", sa.Float(), nullable=True),
        sa.Column("kbit_s", sa.Float(), nullable=True),
        sa.Column("rtt_ms", sa.Float(), nullable=True),
        sa.Column("positions_total", sa.BigInteger(), nullable=False,
                  server_default="0"),
    )
    op.create_index("ix_stations_last_seen", "stations", ["last_seen"])

    op.create_table(
        "station_sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("station_id", sa.BigInteger(),
                  sa.ForeignKey("stations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("peak_msgs_per_s", sa.Float(), nullable=False,
                  server_default="0"),
        sa.Column("positions_total", sa.BigInteger(), nullable=False,
                  server_default="0"),
    )
    op.create_index("ix_station_sessions_station_id", "station_sessions",
                    ["station_id"])
    op.create_index("ix_station_sessions_station_started", "station_sessions",
                    ["station_id", sa.text("started_at DESC")])


def downgrade() -> None:
    op.drop_table("station_sessions")
    op.drop_table("stations")
