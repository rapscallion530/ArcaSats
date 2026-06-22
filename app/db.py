# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
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
    NOTE: we deliberately do NOT enable `PRAGMA foreign_keys=ON` yet. Child cleanup is handled
    by ORM `cascade="all, delete-orphan"` relationships, and turning on strict enforcement
    would require ON DELETE rules (e.g. SET NULL on accounts.owner_user_id) plus a table
    rebuild migration so the documented lockout-reset (deleting `users` rows) still works.
    Tracked as a follow-up in docs/code-review.md.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create tables. Import models first so they register on Base.metadata."""
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()


def _run_lightweight_migrations() -> None:
    """Add columns introduced after a DB was first created (SQLite ADD COLUMN)."""
    additive = {
        "transactions": {
            "carried_basis_usd": "NUMERIC",
            "carry_disabled": "INTEGER DEFAULT 0",
            "fiat_source": "VARCHAR",
        },
        "accounts": {"owner": "VARCHAR DEFAULT ''", "lot_method": "VARCHAR DEFAULT 'fifo'"},
        "users": {"token_version": "INTEGER DEFAULT 0"},
        "wallets": {"onchain_mode": "VARCHAR DEFAULT 'standalone'",
                    "address_type": "VARCHAR DEFAULT 'auto'"},
        "node_config": {"mempool_url": "VARCHAR DEFAULT ''", "price_source": "VARCHAR DEFAULT 'coinbase'"},
    }
    # Indexes added after a DB was first created. create_all() only builds indexes for
    # tables it creates fresh, so existing user DBs need these explicitly. Names must match
    # the model definitions so create_all() doesn't try to re-create them on fresh DBs.
    indexes = {
        "ix_tx_account_ts": "transactions (account_id, timestamp)",
        "ix_tx_txid": "transactions (txid)",
        "ix_tx_kind": "transactions (kind)",
        "ix_wallets_account_id": "wallets (account_id)",
    }
    with engine.begin() as conn:
        for table, cols in additive.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        for name, target in indexes.items():
            conn.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
        # Any pre-existing transaction that already carries a USD value is authoritative
        # (CSV/API import or a manual entry) — mark it so the price backfill never clobbers
        # it. Idempotent: only fills rows whose provenance is still unset.
        conn.exec_driver_sql(
            "UPDATE transactions SET fiat_source='actual' "
            "WHERE fiat_value IS NOT NULL AND (fiat_source IS NULL OR fiat_source='')"
        )
        # On-chain (xpub) reclassification migration (see docs/onchain-classification.md):
        # 1) Normalize the dedupe id from kind-based to DIRECTION-based so a future re-sync that
        #    relabels a tx (e.g. transfer_out -> sell) matches the existing row instead of
        #    inserting a duplicate.
        conn.exec_driver_sql(
            "UPDATE transactions SET external_id = replace(external_id, ':transfer_in', ':in') "
            "WHERE source LIKE 'xpub:%' AND external_id LIKE '%:transfer_in'")
        conn.exec_driver_sql(
            "UPDATE transactions SET external_id = replace(external_id, ':transfer_out', ':out') "
            "WHERE source LIKE 'xpub:%' AND external_id LIKE '%:transfer_out'")
        # 2) Reclassify existing on-chain rows in STANDALONE wallets: a blanket transfer label
        #    was hiding taxable buys/sells. External inflow -> buy, external outflow -> sell.
        #    Idempotent (only rows still labeled transfer_in/out). Genuine cross-wallet transfers
        #    are restored to "transfer" by reconcile_internal_transfers() on the next reconcile.
        conn.exec_driver_sql(
            "UPDATE transactions SET kind='buy' WHERE source LIKE 'xpub:%' AND kind='transfer_in' "
            "AND wallet_id IN (SELECT id FROM wallets WHERE onchain_mode='standalone')")
        conn.exec_driver_sql(
            "UPDATE transactions SET kind='sell' WHERE source LIKE 'xpub:%' AND kind='transfer_out' "
            "AND wallet_id IN (SELECT id FROM wallets WHERE onchain_mode='standalone')")


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
