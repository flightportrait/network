"""Session plumbing. One sessionmaker per app instance, resolved through
app.state — the same convention that lets tests inject SQLite."""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from fastapi import Request


class Base(DeclarativeBase):
    """Metadata root for this service only; alembic/ manages exactly
    this metadata."""


def make_sessionmaker(database_url: str):
    engine = create_engine(database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def get_session(request: Request):
    session = request.app.state.sessionmaker()
    try:
        yield session
    finally:
        session.close()
