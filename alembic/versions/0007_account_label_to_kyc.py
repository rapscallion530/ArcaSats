"""Account label -> KYC status: relocate legacy free-text labels into the note.

The account "Label (optional)" free-text field became a KYC-status dropdown (KYC / Non-KYC). This
normalizes existing values and preserves any custom text by moving it into the account's note
(see app.services.accounts.split_kyc_label). Idempotent: re-running leaves canonical values alone.

Revision ID: 0007_account_label_to_kyc
Revises: 0006_tx_raw_acquired
Create Date: 2026-06-28
"""
from alembic import op
import sqlalchemy as sa

from app.services.accounts import split_kyc_label

revision = "0007_account_label_to_kyc"
down_revision = "0006_tx_raw_acquired"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    cols = {c["name"] for c in sa.inspect(conn).get_columns("accounts")}
    if not {"label_kind", "note"} <= cols:
        return
    for id_, label, note in conn.execute(sa.text("SELECT id, label_kind, note FROM accounts")).fetchall():
        new_label, new_note = split_kyc_label(label or "", note or "")
        if new_label != (label or "") or new_note != (note or ""):
            conn.execute(
                sa.text("UPDATE accounts SET label_kind = :l, note = :n WHERE id = :i"),
                {"l": new_label, "n": new_note, "i": id_},
            )


def downgrade() -> None:
    # Not reversible (custom labels were merged into notes); leave data as-is.
    pass
