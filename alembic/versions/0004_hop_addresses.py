"""hop_addresses: foreign addresses one hop from our coins (address-based fuzzy-hop detection)

Revision ID: 0004_hop_addresses
Revises: 0003_kyc_origin
Create Date: 2026-06-25

Records the destination address of each of our outflows and the funder address of each of our
inflows, so the reconciliation inbox can detect a known->unknown->known self-transfer by a shared
intermediary ADDRESS (robust to amount/time drift) instead of amount+date. Guarded so it's a
no-op on a DB where the table already exists (e.g. created via create_all in tests).
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_hop_addresses"
down_revision = "0003_kyc_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "hop_addresses" in insp.get_table_names():
        return
    op.create_table(
        "hop_addresses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("wallet_id", sa.Integer(), nullable=False),
        sa.Column("txid", sa.String(length=80), nullable=False),
        sa.Column("direction", sa.String(length=3), nullable=False),
        sa.Column("address", sa.String(length=120), nullable=False),
        sa.Column("value_sats", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wallet_id", "txid", "direction", "address",
                            name="uq_hop_wallet_tx_dir_addr"),
    )
    with op.batch_alter_table("hop_addresses", schema=None) as batch_op:
        batch_op.create_index("ix_hop_address", ["address"], unique=False)
        batch_op.create_index("ix_hop_account", ["account_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_hop_addresses_wallet_id"), ["wallet_id"], unique=False)


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "hop_addresses" not in insp.get_table_names():
        return
    with op.batch_alter_table("hop_addresses", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_hop_addresses_wallet_id"))
        batch_op.drop_index("ix_hop_account")
        batch_op.drop_index("ix_hop_address")
    op.drop_table("hop_addresses")
