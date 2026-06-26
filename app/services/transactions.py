# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Transaction operations + input parsing helpers."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import SATS_PER_BTC, Account, Transaction, TxKind

# Direct (external) acquisitions whose KYC provenance is the acquiring account's label. A
# TRANSFER_IN is excluded: its coins came from elsewhere, so its label is the SOURCE's, carried
# in by the reconciler (costbasis.carried_lots), not the destination account's.
_KYC_SNAPSHOT_KINDS = (TxKind.BUY, TxKind.INCOME, TxKind.OPENING)


def btc_to_sats(value: str | Decimal | float) -> int:
    """Parse a BTC amount string to integer sats without float drift."""
    if value in (None, ""):
        return 0
    d = Decimal(str(value))
    return int((d * SATS_PER_BTC).to_integral_value(rounding="ROUND_HALF_UP"))


def parse_usd(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace("$", "").replace(",", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def parse_timestamp(value: str) -> dt.datetime:
    """Accept 'YYYY-MM-DD' or ISO 'YYYY-MM-DDTHH:MM'. Returns naive-UTC."""
    value = (value or "").strip()
    if not value:
        return dt.datetime.now(dt.UTC).replace(tzinfo=None)
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    # Last resort: ISO parse
    return dt.datetime.fromisoformat(value).replace(tzinfo=None)


def add_transaction(
    session: Session,
    *,
    account_id: int,
    kind: str,
    timestamp: dt.datetime,
    amount_sats: int,
    fee_sats: int = 0,
    price_usd: Decimal | None = None,
    fiat_value: Decimal | None = None,
    fiat_fee: Decimal | None = None,
    fiat_source: str | None = None,
    wallet_id: int | None = None,
    txid: str | None = None,
    address: str | None = None,
    counterparty: str = "",
    source: str = "manual",
    external_id: str | None = None,
    note: str = "",
    kyc_origin: str = "",
    acquired_at: dt.datetime | None = None,
    carried_basis_usd: Decimal | None = None,
    raw_import: str | None = None,
    commit: bool = True,
) -> Transaction | None:
    """Insert a transaction. Returns None if it's a duplicate (source+external_id).

    fiat_source records where the USD value came from ("actual"/"manual"); it only sticks
    when a fiat_value is actually present, so the price backfill can fill missing ones.

    kyc_origin snapshots the acquiring account's KYC label onto a direct acquisition
    (buy/income/opening) so the cost-basis engine can report holdings/gains by KYC class. It's
    derived centrally from the account here (so importers/routes need not pass it); an explicit
    value wins. Mirrors how Utxo.label_kind is snapshotted at sync.

    commit=True (default) commits immediately and de-dupes via the (account, source, external_id)
    unique constraint (returns None on conflict). Bulk importers pass commit=False to add many
    rows in one transaction and commit once at the end; they MUST pre-filter duplicates themselves
    (the constraint would otherwise abort the whole batch), and the returned tx is unflushed.
    """
    if kind not in TxKind.ALL:
        raise ValueError(f"unknown kind: {kind}")

    if not kyc_origin and kind in _KYC_SNAPSHOT_KINDS:
        acct = session.get(Account, account_id)  # identity-cached within an import
        kyc_origin = acct.label_kind if acct else ""

    # Derive fiat_value from price if only price given (and vice versa).
    if fiat_value is None and price_usd is not None and amount_sats:
        fiat_value = (price_usd * Decimal(amount_sats) / SATS_PER_BTC).quantize(Decimal("0.01"))
    if price_usd is None and fiat_value is not None and amount_sats:
        price_usd = (fiat_value * SATS_PER_BTC / Decimal(amount_sats)).quantize(Decimal("0.01"))

    tx = Transaction(
        account_id=account_id, wallet_id=wallet_id, kind=kind, timestamp=timestamp,
        amount_sats=amount_sats, fee_sats=fee_sats, price_usd=price_usd,
        fiat_value=fiat_value, fiat_fee=fiat_fee,
        fiat_source=(fiat_source if fiat_value is not None else None),
        txid=txid, address=address, acquired_at=acquired_at,
        carried_basis_usd=carried_basis_usd,
        counterparty=counterparty, source=source, external_id=external_id, note=note,
        kyc_origin=kyc_origin, raw_import=raw_import,
    )
    session.add(tx)
    if not commit:
        return tx  # caller batches the commit (and has pre-filtered duplicates)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return None
    session.refresh(tx)
    return tx


def update_transaction(
    session: Session, tx_id: int, *, kind: str, timestamp: dt.datetime, amount_sats: int,
    fiat_value: Decimal | None = None, fee_sats: int | None = None, counterparty: str = "", note: str = "",
    fiat_source: str | None = "manual", txid: str | None = None, address: str | None = None,
    acquired_at: dt.datetime | None = None, carried_basis_usd: Decimal | None = None,
    set_links: bool = False,
) -> Transaction | None:
    """Edit a transaction's fields, including its kind (e.g. sell -> transfer_out).

    A USD value supplied here is treated as authoritative (fiat_source="manual" by default)
    so a later price backfill won't overwrite it. Clearing the value resets provenance,
    making the tx eligible for re-estimation. When `set_links` is True, the on-chain fields
    (txid/address) and the carryover fields (acquired_at, carried_basis_usd) are also applied —
    so the detail view can correct/add a txid or address to force a wallet link, or override the
    cost basis / acquisition date. (The blank default is ignored unless set_links is True, so
    callers that don't manage those fields don't wipe them.)
    """
    tx = session.get(Transaction, tx_id)
    if tx is None:
        return None
    if kind not in TxKind.ALL:
        raise ValueError(f"unknown kind: {kind}")
    tx.kind = kind
    tx.timestamp = timestamp
    tx.amount_sats = amount_sats
    if fee_sats is not None:  # preserve the existing fee when the edit form doesn't supply one
        tx.fee_sats = fee_sats
    tx.fiat_value = fiat_value
    tx.fiat_source = fiat_source if fiat_value is not None else None
    tx.counterparty = counterparty
    tx.note = note
    if set_links:
        tx.txid = (txid or "").strip() or None
        tx.address = (address or "").strip() or None
        tx.acquired_at = acquired_at
        tx.carried_basis_usd = carried_basis_usd
    # Keep price_usd consistent with fiat_value/amount.
    if fiat_value is not None and amount_sats:
        tx.price_usd = (fiat_value * SATS_PER_BTC / Decimal(amount_sats)).quantize(Decimal("0.01"))
    else:
        tx.price_usd = None
    session.commit()
    session.refresh(tx)
    return tx


def list_transactions(session: Session, account_id: int) -> list[Transaction]:
    return list(
        session.scalars(
            select(Transaction)
            .where(Transaction.account_id == account_id)
            .order_by(Transaction.timestamp, Transaction.id)
        )
    )


def delete_transaction(session: Session, tx_id: int) -> bool:
    tx = session.get(Transaction, tx_id)
    if tx is None:
        return False
    session.delete(tx)
    session.commit()
    return True
