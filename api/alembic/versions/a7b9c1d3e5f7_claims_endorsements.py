"""Claims and endorsements replace the flat contributions table.

A claim is one distinct answer to a question; an endorsement is one
person behind it. Pending rows from the flat table become one claim
each with one endorsement. The catalog points at claims.

Revision ID: a7b9c1d3e5f7
Revises: f6a8b0c2d4e6
Create Date: 2026-09-08
"""
from alembic import op
import sqlalchemy as sa

revision = "a7b9c1d3e5f7"
down_revision = "f6a8b0c2d4e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("callsign", sa.String(12), nullable=False),
        sa.Column("origin", sa.String(4), nullable=False),
        sa.Column("dest", sa.String(4), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("verdict", sa.String(14), nullable=True),
        sa.Column("checks", sa.JSON(), nullable=True),
        sa.Column("anonymous_count", sa.Integer(), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(12), nullable=True),
        sa.Column("review_note", sa.String(280), nullable=True),
        sa.UniqueConstraint("callsign", "origin", "dest",
                            name="uq_claims_route"),
    )
    op.create_index("ix_claims_callsign", "claims", ["callsign"])
    op.create_index("ix_claims_status", "claims", ["status"])
    op.create_table(
        "endorsements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("claim_id", sa.Integer(), sa.ForeignKey("claims.id"),
                  nullable=False),
        sa.Column("handle", sa.String(40), nullable=True),
        sa.Column("note", sa.String(280), nullable=True),
        sa.Column("key_name", sa.String(40), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=True, unique=True),
    )
    op.create_index("ix_endorsements_claim_id", "endorsements", ["claim_id"])
    op.create_index("ix_endorsements_handle", "endorsements", ["handle"])
    op.create_table(
        "pull_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cursor", sa.Integer(), nullable=False),
        sa.Column("pulled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "INSERT INTO claims (callsign, origin, dest, status, verdict, checks,"
        " anonymous_count, first_at, last_at, reviewed_at, review_note)"
        " SELECT callsign, origin, dest, status, 'unverified', checks, 0,"
        " submitted_at, submitted_at, reviewed_at, review_note"
        " FROM contributions WHERE origin IS NOT NULL AND dest IS NOT NULL")
    op.execute(
        "INSERT INTO endorsements (claim_id, handle, note, valid_from,"
        " received_at)"
        " SELECT c.id, k.handle, k.note, k.valid_from, k.submitted_at"
        " FROM contributions k JOIN claims c ON c.callsign = k.callsign"
        " AND c.origin = k.origin AND c.dest = k.dest")
    op.add_column("route_catalog",
                  sa.Column("claim_id", sa.Integer(),
                            sa.ForeignKey("claims.id"), nullable=True))
    with op.batch_alter_table("route_catalog") as batch:
        batch.drop_column("contribution_id")
    op.drop_table("contributions")


def downgrade() -> None:
    raise RuntimeError("the flat contributions table is not restored")
