"""mempool_use_tor: route the app's mempool price API over Tor (for a .onion mempool)

Revision ID: 0005_mempool_use_tor
Revises: 0004_hop_addresses
Create Date: 2026-06-26

Adds node_config.mempool_use_tor so the mempool connection is configured/tested independently of
the Electrum node and can reach a .onion mempool via the shared Tor SOCKS proxy. Guarded so it's a
no-op when the column already exists (e.g. a fresh DB built by create_all in tests).
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_mempool_use_tor"
down_revision = "0004_hop_addresses"
branch_labels = None
depends_on = None


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "node_config" in insp.get_table_names() and "mempool_use_tor" not in _columns(insp, "node_config"):
        op.add_column("node_config",
                      sa.Column("mempool_use_tor", sa.Boolean(), nullable=False,
                                server_default=sa.false()))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "node_config" in insp.get_table_names() and "mempool_use_tor" in _columns(insp, "node_config"):
        with op.batch_alter_table("node_config") as batch_op:
            batch_op.drop_column("mempool_use_tor")
