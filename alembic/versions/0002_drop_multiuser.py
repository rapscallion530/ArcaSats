"""drop orphaned multi-user + allow_remote schema from pre-Alembic DBs

Revision ID: 0002_drop_multiuser
Revises: 0001_baseline
Create Date: 2026-06-25

The baseline (0001) already describes the clean single-user schema, so on a FRESH DB this is a
no-op. On an existing pre-Alembic DB (stamped at baseline on first run), it drops the leftovers
from the multi-user removal and the earlier allow_remote removal. Every step is guarded so it's
idempotent and safe to run against either state.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_drop_multiuser"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    # Obsolete multi-user table (single-user app now).
    if "users" in tables:
        op.drop_table("users")

    # Obsolete Account.owner_user_id FK column — `owner` is now a plain string label.
    if "accounts" in tables and "owner_user_id" in _columns(insp, "accounts"):
        with op.batch_alter_table("accounts") as batch_op:
            batch_op.drop_column("owner_user_id")

    # Obsolete LLMConnection.allow_remote flag — assistant locality is env-gated (services/llm.py).
    if "llm_connections" in tables and "allow_remote" in _columns(insp, "llm_connections"):
        with op.batch_alter_table("llm_connections") as batch_op:
            batch_op.drop_column("allow_remote")


def downgrade() -> None:
    # Best-effort restore of the dropped columns/table (their data is intentionally discarded
    # going forward — single-user has no users and no owner_user_id/allow_remote).
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "accounts" in tables and "owner_user_id" not in _columns(insp, "accounts"):
        with op.batch_alter_table("accounts") as batch_op:
            batch_op.add_column(sa.Column("owner_user_id", sa.Integer(), nullable=True))

    if "llm_connections" in tables and "allow_remote" not in _columns(insp, "llm_connections"):
        with op.batch_alter_table("llm_connections") as batch_op:
            batch_op.add_column(sa.Column("allow_remote", sa.Boolean(), nullable=False,
                                          server_default=sa.false()))

    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=60), nullable=False),
            sa.Column("password_hash", sa.String(length=255), nullable=False),
            sa.Column("role", sa.String(length=20), nullable=False),
            sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("username"),
        )
