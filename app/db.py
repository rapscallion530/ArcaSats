# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""SQLAlchemy engine, session, and base."""
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """Tune SQLite for a small concurrent web app on a single file:
      - WAL: readers don't block the writer (HTMX fires several requests at once);
      - synchronous=NORMAL: safe with WAL and much faster than FULL.
    NOTE: we do NOT enable `PRAGMA foreign_keys=ON` here. Child cleanup is handled by ORM
    `cascade="all, delete-orphan"` relationships; enabling strict enforcement needs ON DELETE
    rules + a table-rebuild migration (planned for the Alembic adoption).
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Bring the schema to head via Alembic. Importing models first registers them so the
    Alembic env (and a fresh baseline) sees the full metadata."""
    from app import models  # noqa: F401

    _run_migrations()


def _run_migrations() -> None:
    """Apply Alembic migrations to the configured DB. A pre-Alembic DB (tables present but no
    `alembic_version`) is first STAMPED at the baseline, so only the newer migrations run on it;
    a fresh DB is built from scratch by the baseline. Idempotent — a no-op once at head."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import inspect

    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))

    with engine.begin() as conn:
        already_stamped = MigrationContext.configure(conn).get_current_revision() is not None
        predates_alembic = inspect(conn).has_table("accounts") and not already_stamped
    if predates_alembic:
        command.stamp(cfg, "0001_baseline")  # adopt the existing schema as the baseline
    command.upgrade(cfg, "head")


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
