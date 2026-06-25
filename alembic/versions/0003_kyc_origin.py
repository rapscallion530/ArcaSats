"""KYC origin on lots + fragment-rebuild carry + KYC-aware disposal priority

Revision ID: 0003_kyc_origin
Revises: 0002_drop_multiuser
Create Date: 2026-06-25

Layer A (KYC on the lot) + start of Layer B (dispose by KYC status):
  - transactions.kyc_origin  — provenance snapshot of the account's label at acquisition.
  - transactions.carried_lots — JSON source-lot fragments for fragment-rebuild basis carry.
  - accounts.disposal_priority — KYC-aware lot-selection policy (none/non_kyc_first/kyc_first).

Backfill: snapshot the account's current label_kind onto existing DIRECT acquisitions
(buy/income/opening). Transfer-ins are left blank — their label is the source's, populated on
the next reconcile/Sync (which now writes carried_lots). Every step is guarded so it's
idempotent against a fresh baseline DB (where the columns may already exist via create_all).
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_kyc_origin"
down_revision = "0002_drop_multiuser"
branch_labels = None
depends_on = None


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tx_cols = _columns(insp, "transactions")
    acct_cols = _columns(insp, "accounts")

    if "kyc_origin" not in tx_cols:
        op.add_column("transactions",
                      sa.Column("kyc_origin", sa.String(length=40), nullable=False,
                                server_default=""))
    if "carried_lots" not in tx_cols:
        op.add_column("transactions", sa.Column("carried_lots", sa.Text(), nullable=True))
    if "disposal_priority" not in acct_cols:
        op.add_column("accounts",
                      sa.Column("disposal_priority", sa.String(length=16), nullable=False,
                                server_default="none"))

    # Snapshot the account's label onto existing direct acquisitions (transfer_ins stay blank).
    op.execute(
        "UPDATE transactions SET kyc_origin = COALESCE("
        "  (SELECT label_kind FROM accounts WHERE accounts.id = transactions.account_id), '')"
        " WHERE kind IN ('buy', 'income', 'opening')"
    )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "disposal_priority" in _columns(insp, "accounts"):
        with op.batch_alter_table("accounts") as batch_op:
            batch_op.drop_column("disposal_priority")
    tx_cols = _columns(insp, "transactions")
    with op.batch_alter_table("transactions") as batch_op:
        if "carried_lots" in tx_cols:
            batch_op.drop_column("carried_lots")
        if "kyc_origin" in tx_cols:
            batch_op.drop_column("kyc_origin")
