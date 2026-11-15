"""Database engine, session factory and declarative base."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

logger = logging.getLogger("arcusclips.db")


def utcnow() -> datetime:
    """Naive UTC timestamp (portable across SQLite/Postgres)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine
    url = settings.resolved_database_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        db_file = url.split("sqlite:///")[-1]
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    _engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _):  # WAL for concurrent worker+API
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def session_scope() -> Session:
    """A new session; caller is responsible for close/commit/rollback."""
    return get_session_factory()()


def get_db():
    """FastAPI dependency yielding a scoped session."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create all tables (idempotent). Alembic handles migrations in production."""
    from . import models  # noqa: F401  (ensure models are registered)

    Base.metadata.create_all(get_engine())
    logger.info("Database schema ready at %s", settings.resolved_database_url)
