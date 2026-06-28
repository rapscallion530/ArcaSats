# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Alembic environment. Targets the app's SQLAlchemy metadata and the same SQLite URL the app
uses (from app.config), with batch mode on so SQLite ALTERs (drop column, change constraints)
work via table rebuilds."""
from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  (register all tables on Base.metadata)
from app.config import DATABASE_URL
from app.db import Base

# NB: we intentionally do NOT call logging.config.fileConfig here — migrations run embedded at
# app startup (db.init_db), and reconfiguring logging there would clobber the app's loggers.
config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=DATABASE_URL, target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = DATABASE_URL
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
