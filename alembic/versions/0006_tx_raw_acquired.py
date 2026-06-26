"""tx raw-import stash + acquired_at (lossless CSV mapping + custodian acquisition-date)

Revision ID: 0006_tx_raw_acquired
Revises: 0005_mempool_use_tor
Create Date: 2026-06-26

  - transactions.raw_import  — JSON of the original CSV row (nothing the export offers is lost).
  - transactions.acquired_at — lot holding-period origin override (honors a custodian-provided
    acquisition date on a transferred-in coin).

Guarded so it's a no-op when the columns already exist (fresh DB built via create_all in tests).
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_tx_raw_acquired"
down_revision = "0005_mempool_use_tor"
branch_labels = None
depends_on = None


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = _columns(insp, "transactions")
    if "acquired_at" not in cols:
        op.add_column("transactions", sa.Column("acquired_at", sa.DateTime(), nullable=True))
    if "raw_import" not in cols:
        op.add_column("transactions", sa.Column("raw_import", sa.Text(), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = _columns(insp, "transactions")
    with op.batch_alter_table("transactions") as batch_op:
        if "raw_import" in cols:
            batch_op.drop_column("raw_import")
        if "acquired_at" in cols:
            batch_op.drop_column("acquired_at")
