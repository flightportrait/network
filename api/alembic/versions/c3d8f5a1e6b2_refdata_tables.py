"""reference data tables

Revision ID: c3d8f5a1e6b2
Revises: b7e1a2c9d4f0
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d8f5a1e6b2"
down_revision = "b7e1a2c9d4f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ref_airframes",
        sa.Column("hex", sa.String(6), primary_key=True),
        sa.Column("registration", sa.String(16), nullable=True),
        sa.Column("type_code", sa.String(8), nullable=True),
        sa.Column("operator_name", sa.String(120), nullable=True),
        sa.Column("operator_norm", sa.String(120), nullable=True),
        sa.Column("operator_icao", sa.String(3), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("flags", sa.String(8), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ref_airframes_operator_norm", "ref_airframes",
                    ["operator_norm"])
    op.create_index("ix_ref_airframes_operator_icao", "ref_airframes",
                    ["operator_icao"])

    op.create_table(
        "ref_airlines",
        sa.Column("icao", sa.String(3), primary_key=True),
        sa.Column("iata", sa.String(2), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("palette", sa.JSON(), nullable=True),
    )

    op.create_table(
        "ref_types",
        sa.Column("designator", sa.String(4), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("category", sa.String(12), nullable=True),
    )

    op.create_table(
        "ref_airports",
        sa.Column("ident", sa.String(8), primary_key=True),
        sa.Column("name", sa.String(120), nullable=True),
        sa.Column("kind", sa.String(20), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("iso_country", sa.String(2), nullable=True),
        sa.Column("municipality", sa.String(80), nullable=True),
        sa.Column("iata", sa.String(3), nullable=True),
    )

    op.create_table(
        "ref_routes",
        sa.Column("callsign", sa.String(12), primary_key=True),
        sa.Column("chain", sa.JSON(), nullable=False),
        sa.Column("airline_icao", sa.String(3), nullable=True),
    )
    op.create_index("ix_ref_routes_airline_icao", "ref_routes",
                    ["airline_icao"])

    op.create_table(
        "ref_airline_countries",
        sa.Column("airline_icao", sa.String(3), primary_key=True),
        sa.Column("iso_country", sa.String(2), primary_key=True),
        sa.Column("n_routes", sa.Integer(), nullable=False),
    )

    op.create_table(
        "ref_imports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True),
                  nullable=False),
    )


def downgrade() -> None:
    for table in ("ref_imports", "ref_airline_countries", "ref_routes",
                  "ref_airports", "ref_types", "ref_airlines",
                  "ref_airframes"):
        op.drop_table(table)
