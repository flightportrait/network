"""airframes: a serial number is not an identity on its own

(type, msn) is not unique in the world: Dassault restarts serials per
variant under one ICAO type (Falcon 2000 and 2000EX both have no. 003),
and homebuilts reuse "1". The unique constraint becomes a plain index;
same-aircraft matching (AIRFRAMES.md D2) needs manufacturer, model and
serial from a named source and is reviewed, not enforced by the schema.

Revision ID: f4b6d8e0a2c4
Revises: e2a4c6e8f0b2
Create Date: 2026-09-23
"""
from alembic import op

revision = "f4b6d8e0a2c4"
down_revision = "e2a4c6e8f0b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("airframes") as batch:
        batch.drop_constraint("uq_airframes_type_msn", type_="unique")
    op.create_index("ix_airframes_msn", "airframes", ["msn"])


def downgrade() -> None:
    op.drop_index("ix_airframes_msn", "airframes")
    with op.batch_alter_table("airframes") as batch:
        batch.create_unique_constraint("uq_airframes_type_msn",
                                       ["type_code", "msn"])
