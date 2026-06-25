# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Account & wallet operations."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Account, Transaction, TxKind, Wallet

# Outflows for balance math (disposals + moves out + standalone network fees). Acquisitions
# are inflows. A standalone FEE tx's amount_sats leaves the wallet, so it reduces the balance.
_OUTFLOW_KINDS = (TxKind.SELL, TxKind.SPEND, TxKind.TRANSFER_OUT, TxKind.FEE)


def _balance_expr():
    """SQL expression for an account's net sats = inflows - outflows - fees, usable in a
    grouped aggregate so we don't issue a query per account."""
    inflow = func.sum(case((Transaction.kind.in_(TxKind.ACQUISITIONS), Transaction.amount_sats), else_=0))
    outflow = func.sum(case((Transaction.kind.in_(_OUTFLOW_KINDS), Transaction.amount_sats), else_=0))
    fees = func.sum(Transaction.fee_sats)
    return func.coalesce(inflow, 0) - func.coalesce(outflow, 0) - func.coalesce(fees, 0)


@dataclass
class AccountSummary:
    account: Account
    wallet_count: int
    tx_count: int
    balance_sats: int


def create_account(session: Session, name: str, label_kind: str = "", note: str = "",
                   owner: str = "") -> Account:
    acct = Account(name=name.strip(), label_kind=label_kind.strip(), note=note.strip(),
                   owner=owner.strip())
    session.add(acct)
    session.commit()
    session.refresh(acct)
    return acct


def list_accounts(session: Session) -> list[Account]:
    return list(session.scalars(select(Account).order_by(Account.name)))


def get_account(session: Session, account_id: int) -> Account | None:
    return session.get(Account, account_id)


def get_tx(session: Session, account_id: int, tx_id: int) -> Transaction | None:
    """A transaction that belongs to `account_id` (else None) — keeps the URL's account and tx
    consistent, so /accounts/<a>/transactions/<id-from-another-account> resolves to nothing."""
    tx = session.get(Transaction, tx_id)
    return tx if tx is not None and tx.account_id == account_id else None


def update_account(session: Session, account_id: int, name: str, label_kind: str = "",
                   note: str = "", owner: str = "", lot_method: str = "") -> tuple[Account | None, str]:
    """Edit an account's name, label, owner, and note. Returns (account, error).
    Name must be non-empty and unique."""
    from sqlalchemy.exc import IntegrityError
    acct = session.get(Account, account_id)
    if acct is None:
        return None, "account not found"
    name = name.strip()
    if not name:
        return acct, "Name cannot be empty."
    acct.name = name
    acct.label_kind = label_kind.strip()
    acct.owner = owner.strip()
    if lot_method in ("fifo", "lifo", "hifo"):
        acct.lot_method = lot_method
    acct.note = note.strip()
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return session.get(Account, account_id), "An account with that name already exists."
    session.refresh(acct)
    return acct, ""


def delete_account(session: Session, account_id: int) -> bool:
    acct = session.get(Account, account_id)
    if acct is None:
        return False
    session.delete(acct)
    session.commit()
    return True


def add_wallet(session: Session, account_id: int, label: str, wtype: str, **kw) -> Wallet:
    w = Wallet(account_id=account_id, label=label.strip(), wtype=wtype, **kw)
    session.add(w)
    session.commit()
    session.refresh(w)
    return w


def list_wallets(session: Session, account_id: int) -> list[Wallet]:
    return list(session.scalars(select(Wallet).where(Wallet.account_id == account_id).order_by(Wallet.id)))


def get_wallet(session: Session, wallet_id: int) -> Wallet | None:
    return session.get(Wallet, wallet_id)


def validate_key_or_descriptor(value: str) -> str:
    """Return an error string if `value` is neither a valid extended key nor a parseable
    multisig descriptor; '' if it's fine."""
    from app.services import descriptor as desc_mod
    from app.services.bip32 import key_kind
    value = (value or "").strip()
    if not value:
        return ""
    if desc_mod.is_descriptor(value):
        try:
            desc_mod.parse_descriptor(value)
            return ""
        except ValueError as exc:
            return f"Unrecognized multisig descriptor: {exc}"
    try:
        key_kind(value)
        return ""
    except ValueError:
        return ("Unrecognized key — paste an xpub/ypub/zpub (single-sig) or a multisig "
                "descriptor like wsh(sortedmulti(2,…)).")


def update_wallet(session: Session, wallet_id: int, label: str, xpub: str, gap_limit: int,
                  onchain_mode: str | None = None, address_type: str | None = None) -> tuple[Wallet | None, str]:
    """Edit a wallet's label, key/descriptor, gap limit, classification mode, and address-type
    override. Returns (wallet, error). Changing the key resets the detected script type."""
    w = session.get(Wallet, wallet_id)
    if w is None:
        return None, "wallet not found"
    label = label.strip()
    xpub = xpub.strip()
    if not label:
        return w, "Label cannot be empty."
    if w.wtype == "xpub" and xpub:
        err = validate_key_or_descriptor(xpub)
        if err:
            return w, err
        if xpub != (w.xpub or ""):
            w.script_type = ""  # re-detect on next sync
    w.label = label
    if w.wtype == "xpub":
        w.xpub = xpub or None
    w.gap_limit = max(1, min(int(gap_limit or 20), 100))
    if onchain_mode in ("standalone", "custodial_fed"):
        w.onchain_mode = onchain_mode
    if address_type in ("auto", "p2wpkh", "p2sh-p2wpkh", "p2pkh"):
        w.address_type = address_type
    session.commit()
    session.refresh(w)
    return w, ""


def delete_wallet(session: Session, wallet_id: int) -> int | None:
    """Delete a wallet and its imported transactions. Returns the account_id, or None."""
    w = session.get(Wallet, wallet_id)
    if w is None:
        return None
    account_id = w.account_id
    session.delete(w)  # cascade removes the wallet's transactions
    session.commit()
    return account_id


def balance_sats(session: Session, account_id: int) -> int:
    """Net BTC balance for an account, in sats (inflows minus outflows minus fees).
    One round-trip via conditional aggregation."""
    row = session.execute(
        select(func.count(Transaction.id), _balance_expr())
        .where(Transaction.account_id == account_id)
    ).one()
    return int(row[1] or 0)


def summarize(session: Session, account: Account) -> AccountSummary:
    """Single-account summary. For lists prefer all_summaries(), which batches the queries."""
    wc = session.scalar(select(func.count(Wallet.id)).where(Wallet.account_id == account.id)) or 0
    row = session.execute(
        select(func.count(Transaction.id), _balance_expr())
        .where(Transaction.account_id == account.id)
    ).one()
    return AccountSummary(account=account, wallet_count=int(wc),
                          tx_count=int(row[0] or 0), balance_sats=int(row[1] or 0))


def all_summaries(session: Session) -> list[AccountSummary]:
    """Summaries for all accounts in a fixed number of queries (no per-account fan-out): one
    grouped query for tx count + balance, one for wallet counts."""
    accounts = list_accounts(session)
    if not accounts:
        return []
    ids = [a.id for a in accounts]

    tx_rows = session.execute(
        select(Transaction.account_id, func.count(Transaction.id), _balance_expr())
        .where(Transaction.account_id.in_(ids))
        .group_by(Transaction.account_id)
    ).all()
    tx_by_acct = {aid: (int(cnt or 0), int(bal or 0)) for aid, cnt, bal in tx_rows}

    wc_rows = session.execute(
        select(Wallet.account_id, func.count(Wallet.id))
        .where(Wallet.account_id.in_(ids))
        .group_by(Wallet.account_id)
    ).all()
    wc_by_acct = {aid: int(cnt or 0) for aid, cnt in wc_rows}

    out = []
    for a in accounts:
        tc, bal = tx_by_acct.get(a.id, (0, 0))
        out.append(AccountSummary(account=a, wallet_count=wc_by_acct.get(a.id, 0),
                                  tx_count=tc, balance_sats=bal))
    return out
