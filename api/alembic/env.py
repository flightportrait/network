"""Alembic environment for the network API's own database — this chain
manages app.models and nothing else."""
import os
import sys

from alembic import context
from sqlalchemy import create_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import Base            # noqa: E402
from app import models             # noqa: E402,F401 — registers tables
from app import refdata_models     # noqa: E402,F401 — registers tables
from app.settings import Settings  # noqa: E402

target_metadata = Base.metadata


def _database_url() -> str:
    return os.environ.get("NETWORK_API_DATABASE_URL") or Settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), target_metadata=target_metadata,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    with engine.connect() as connection:
        context.configure(connection=connection,
                          target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
