"""nat_messages: the North Atlantic track messages as published

Additive: one new table; the downgrade drops it.

Revision ID: c9e1a3b5d7f0
Revises: b5d7f9a1c3e4
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "c9e1a3b5d7f0"
down_revision = "b5d7f9a1c3e4"
branch_labels = None
depends_on = None

BIGID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "nat_messages",
        sa.Column("id", BIGID, primary_key=True, autoincrement=True),
        sa.Column("issuer", sa.String(8), nullable=False),
        sa.Column("tmi", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tracks", sa.JSON(), nullable=False),
        sa.Column("raw", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("issuer", "valid_from", name="uq_nat_messages"),
    )
    op.create_index("ix_nat_messages_valid_from", "nat_messages", ["valid_from"])


def downgrade() -> None:
    op.drop_table("nat_messages")
