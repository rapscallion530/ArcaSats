# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""UTXO inventory + privacy analysis for on-chain wallets.

This is "chain analysis turned inward": it reasons only about coins the user owns (loaded
via xpub/descriptor) to surface coin provenance and privacy exposure — never third parties.
It reads the `utxos` table populated by the xpub scanner and does not touch the tax engine.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SATS_PER_BTC, Utxo


@dataclass
class CoinWarning:
    level: str            # "warn" (privacy-degrading) | "info" (awareness)
    title: str
    detail: str
    txids: list[str] = field(default_factory=list)


def list_utxos(session: Session, account_id: int, *, unspent_only: bool = True) -> list[Utxo]:
    """The account's coins, newest first. unspent_only=True returns the live UTXO set."""
    q = select(Utxo).where(Utxo.account_id == account_id)
    if unspent_only:
        q = q.where(Utxo.spent_txid.is_(None))
    return list(session.scalars(q.order_by(Utxo.created_at.desc(), Utxo.id.desc())))


def unspent_total_sats(utxos: list[Utxo]) -> int:
    return sum(u.value_sats for u in utxos)


def _norm(label: str | None) -> str:
    return (label or "").strip().lower()


def privacy_warnings(session: Session, account_id: int) -> list[CoinWarning]:
    """Privacy lints over the account's coins:

    1. KYC/non-KYC merge — a single on-chain spend that co-spent inputs carrying two or more
       distinct provenance labels publicly links those identities (common-input-ownership).
    2. Address reuse — an address that received funds more than once is trivially clusterable.
    3. Toxic change (info) — each unspent change output is already linked to the payment that
       created it; spending it alongside unrelated coins extends that linkage.
    """
    warnings: list[CoinWarning] = []
    mine = list(session.scalars(select(Utxo).where(Utxo.account_id == account_id)))

    # 1) KYC/non-KYC merge — examine every spend that consumed any of this account's coins,
    #    pulling in the co-spent inputs from ALL accounts (a merge can cross account boundaries).
    spend_txids = {u.spent_txid for u in mine if u.spent_txid}
    if spend_txids:
        inputs_by_spend: dict[str, list[Utxo]] = defaultdict(list)
        for u in session.scalars(select(Utxo).where(Utxo.spent_txid.in_(spend_txids))):
            inputs_by_spend[u.spent_txid].append(u)
        for stxid, inputs in inputs_by_spend.items():
            labels = {_norm(u.label_kind) for u in inputs if _norm(u.label_kind)}
            if len(labels) > 1:
                warnings.append(CoinWarning(
                    level="warn", title="KYC / non-KYC coins merged",
                    detail=(f"A spend co-spent coins labeled {', '.join(sorted(labels))} in one "
                            f"transaction, publicly linking those wallets."),
                    txids=[stxid]))

    # 2) Address reuse (count every receipt at each address, spent or not).
    addr_counts = Counter(u.address for u in mine if u.address)
    reused = sorted(a for a, c in addr_counts.items() if c > 1)
    if reused:
        warnings.append(CoinWarning(
            level="warn", title="Address reuse",
            detail=(f"{len(reused)} address(es) received funds more than once, making them "
                    f"trivially clusterable. Use a fresh address per receipt."),
            txids=[]))

    # 3) Toxic change (informational) — live change outputs.
    change = [u for u in mine if u.is_change and u.spent_txid is None]
    if change:
        btc = sum(u.value_sats for u in change) / SATS_PER_BTC
        warnings.append(CoinWarning(
            level="info", title="Change outputs in inventory",
            detail=(f"{len(change)} unspent change output(s) (~{btc:.8f} BTC). Each is already "
                    f"linked to the payment that created it; consolidating them or spending "
                    f"them with unrelated coins widens that link."),
            txids=[]))

    return warnings
