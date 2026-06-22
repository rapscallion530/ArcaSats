# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""FIFO cost-basis engine (per account / per wallet).

Implements per-account FIFO consistent with IRS Rev. Proc. 2024-28 (per-wallet
basis from 2025-01-01). Acquisitions (buy/income/transfer_in) open lots; taxable
disposals (sell/spend) consume lots FIFO and realize gain split into short/long
term; transfers out consume lots without realizing gain (basis leaves).

All USD math uses Decimal. BTC amounts are integer sats.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SATS_PER_BTC, Account, Transaction, TxKind
from app.services import transactions as tx_svc


def _norm_owner(owner: str | None) -> str:
    return (owner or "").strip().lower()


def tx_key(tx: Transaction) -> str | None:
    """Stable key for a transfer_out's consumed-basis record (txid, or #id fallback
    for exchange withdrawals that carry no on-chain txid)."""
    return tx.txid or (f"#{tx.id}" if tx.id else None)


def internal_txids(session: Session) -> set[str]:
    """Txids that are SELF-transfers between the *same owner's* wallets — an on-chain tx
    that appears as both a transfer_out and a transfer_in whose accounts share an owner.

    A transfer to a different owner (e.g. a family member's xpub) is NOT internal: it's a
    gift/disposal for the sender, and the recipient establishes a fresh basis.
    """
    rows = session.execute(
        select(Transaction.txid, Transaction.kind, Account.owner)
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.txid.is_not(None),
               Transaction.kind.in_((TxKind.TRANSFER_IN, TxKind.TRANSFER_OUT)))
    ).all()
    out_owners: dict[str, set] = defaultdict(set)
    in_owners: dict[str, set] = defaultdict(set)
    for txid, kind, owner in rows:
        (out_owners if kind == TxKind.TRANSFER_OUT else in_owners)[txid].add(_norm_owner(owner))
    # internal only if the SAME owner is on both sides of the txid
    return {txid for txid in (out_owners.keys() & in_owners.keys())
            if out_owners[txid] & in_owners[txid]}

_CENTS = Decimal("0.01")


def _is_long_term(acquired: dt.datetime, disposed: dt.datetime) -> bool:
    """IRS long-term = held MORE THAN one year. Holding period is measured in CALENDAR DATES,
    not clock time: a sale at any time on the one-year anniversary is still short-term; it must
    be on a LATER date. (A Feb-29 acquisition's anniversary is treated as Mar 1.) Comparing
    dates also avoids the `days > 365` leap-year misclassification."""
    a = acquired.date()
    try:
        anniversary = a.replace(year=a.year + 1)
    except ValueError:  # Feb 29 -> no Feb 29 next year
        anniversary = a.replace(year=a.year + 1, month=3, day=1)
    return disposed.date() > anniversary


@dataclass
class Lot:
    acquired: dt.datetime
    sats: int
    basis_usd: Decimal  # basis attributable to the remaining sats
    source: str = ""


@dataclass
class Disposal:
    date: dt.datetime
    kind: str
    sats: int
    proceeds_usd: Decimal
    basis_usd: Decimal
    acquired: dt.datetime
    term: str  # "short" | "long"

    @property
    def gain_usd(self) -> Decimal:
        return (self.proceeds_usd - self.basis_usd).quantize(_CENTS)


@dataclass
class CostBasisResult:
    disposals: list[Disposal] = field(default_factory=list)
    open_lots: list[Lot] = field(default_factory=list)
    income_usd: Decimal = Decimal("0.00")
    warnings: list[str] = field(default_factory=list)
    # txid -> basis consumed by a transfer_out (for cross-account basis carry)
    transfer_out_basis: dict[str, Decimal] = field(default_factory=dict)
    # txid -> list of consumed lot fragments {acquired, sats, basis} (for gift statements)
    transfer_out_lots: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def holding_sats(self) -> int:
        return sum(lot.sats for lot in self.open_lots)

    @property
    def holding_basis_usd(self) -> Decimal:
        return sum((lot.basis_usd for lot in self.open_lots), Decimal("0")).quantize(_CENTS)

    @property
    def avg_cost_per_unit_usd(self) -> Decimal:
        """Average acquisition cost per WHOLE BTC of the coins still held = total basis ÷
        quantity. This is the per-unit 'cost basis' investors quote casually; the tax basis is
        the total above. 0 when nothing is held (avoids div-by-zero)."""
        sats = self.holding_sats
        if sats <= 0:
            return Decimal("0.00")
        return (self.holding_basis_usd / (Decimal(sats) / SATS_PER_BTC)).quantize(_CENTS)

    @property
    def realized_short_usd(self) -> Decimal:
        return sum((d.gain_usd for d in self.disposals if d.term == "short"), Decimal("0")).quantize(_CENTS)

    @property
    def realized_long_usd(self) -> Decimal:
        return sum((d.gain_usd for d in self.disposals if d.term == "long"), Decimal("0")).quantize(_CENTS)

    @property
    def realized_total_usd(self) -> Decimal:
        return (self.realized_short_usd + self.realized_long_usd).quantize(_CENTS)

    @property
    def proceeds_total_usd(self) -> Decimal:
        return sum((d.proceeds_usd for d in self.disposals), Decimal("0")).quantize(_CENTS)


def _acq_basis(tx: Transaction) -> Decimal:
    fee = tx.fiat_fee or Decimal("0")
    # TRANSFER_IN is checked FIRST and NEVER uses fiat_value: a transfer is a non-taxable move,
    # so its basis is the ORIGINAL purchase cost that carries with the coins (supplied by the
    # reconciler), not the market value at receipt. Exchange "receive" CSV rows frequently carry
    # a receipt-time USD value; honoring it here would silently fabricate basis at FMV (a real
    # tax error). With no carryover yet, basis is 0 and compute() warns the user to reconcile or
    # add an Opening-balance lot for outside-acquired coins.
    if tx.kind == TxKind.TRANSFER_IN:
        if tx.carried_basis_usd is not None and not tx.carry_disabled:
            return tx.carried_basis_usd
        return Decimal("0")
    # Buys/income/opening: the recorded USD value is the basis (income FMV = basis = income).
    if tx.fiat_value is not None:
        return tx.fiat_value + (fee if tx.kind == TxKind.BUY else Decimal("0"))
    # Price fallback applies only to true acquisitions (buy/income).
    if tx.kind in (TxKind.BUY, TxKind.INCOME) and tx.price_usd is not None:
        return (tx.price_usd * Decimal(tx.amount_sats) / SATS_PER_BTC)
    return Decimal("0")


LOT_METHODS = ("fifo", "lifo", "hifo")


def _select_index(lots: list[Lot], method: str) -> int:
    """Index of the lot to consume next for the chosen method."""
    if method == "lifo":
        return len(lots) - 1
    if method == "hifo":  # highest cost basis per sat first (minimizes gain)
        best_rate, best_i = None, 0
        for i, lot in enumerate(lots):
            rate = (lot.basis_usd / lot.sats) if lot.sats else Decimal("0")
            if best_rate is None or rate > best_rate:
                best_rate, best_i = rate, i
        return best_i
    return 0  # fifo — lots are kept in acquisition order


def compute(txs: list[Transaction], internal_txids: set[str] | None = None,
            method: str = "fifo", account_internal_within: set[str] | None = None) -> CostBasisResult:
    """Run the lot engine over `txs`.

    `account_internal_within` is the set of txids that are internal moves between wallets of
    the SAME account; those are skipped (no basis change). It MUST be computed at the account
    level: when computing a single wallet's view, the wallet's own tx subset only contains one
    side of an intra-account move, so deriving it from `txs` here would miss it and the
    per-wallet basis would double-count. Callers computing a whole account can leave it None
    (we derive it from `txs`, which then contains both sides).
    """
    res = CostBasisResult()
    lots: list[Lot] = []
    method = method if method in LOT_METHODS else "fifo"
    internal = internal_txids or set()
    if account_internal_within is not None:
        internal_within = account_internal_within
    else:
        # Self-transfers with BOTH sides inside THIS account are internal churn (coins moved
        # between the account's own wallets) — skip them entirely so basis is preserved.
        here_out = {t.txid for t in txs if t.kind == TxKind.TRANSFER_OUT and t.txid}
        here_in = {t.txid for t in txs if t.kind == TxKind.TRANSFER_IN and t.txid}
        internal_within = here_out & here_in & internal

    for tx in sorted(txs, key=lambda t: (t.timestamp, t.id or 0)):
        if tx.txid and tx.txid in internal_within and tx.kind in (TxKind.TRANSFER_IN, TxKind.TRANSFER_OUT):
            continue  # internal move within this account — no basis change

        if tx.kind in TxKind.ACQUISITIONS:
            basis = _acq_basis(tx)
            if tx.kind == TxKind.INCOME:
                res.income_usd += basis
            if tx.kind == TxKind.TRANSFER_IN and basis == 0:
                if tx.txid and tx.txid in internal:
                    res.warnings.append(
                        f"internal transfer in on {tx.timestamp:%Y-%m-%d} (from another of your "
                        f"accounts) — set its cost basis to carry the original purchase price."
                    )
                else:
                    res.warnings.append(
                        f"transfer in on {tx.timestamp:%Y-%m-%d} has no cost basis — set the original "
                        f"acquisition cost so gains compute correctly."
                    )
            if tx.amount_sats > 0:
                lots.append(Lot(acquired=tx.timestamp, sats=tx.amount_sats, basis_usd=basis, source=tx.source))

        elif tx.kind in (TxKind.SELL, TxKind.SPEND):
            proceeds = (tx.fiat_value or Decimal("0"))
            if tx.kind == TxKind.SELL and tx.fiat_fee:
                proceeds -= tx.fiat_fee
            _dispose(lots, tx, proceeds, res, realize=True, method=method)

        elif tx.kind == TxKind.TRANSFER_OUT:
            _dispose(lots, tx, Decimal("0"), res, realize=False, method=method)
        # FEE: ignored for basis

    res.open_lots = list(lots)
    return res


def _dispose(lots: list[Lot], tx: Transaction, proceeds: Decimal, res: CostBasisResult,
             realize: bool, method: str = "fifo") -> None:
    need = tx.amount_sats
    total = need
    consumed_basis = Decimal("0")
    while need > 0 and lots:
        idx = _select_index(lots, method)
        lot = lots[idx]
        take = min(lot.sats, need)
        basis_portion = (lot.basis_usd * Decimal(take) / Decimal(lot.sats)) if lot.sats else Decimal("0")
        consumed_basis += basis_portion
        _k = tx_key(tx)
        if not realize and _k:
            res.transfer_out_lots.setdefault(_k, []).append(
                {"acquired": lot.acquired, "sats": take, "basis": basis_portion.quantize(_CENTS)}
            )
        if realize:
            proceeds_portion = (proceeds * Decimal(take) / Decimal(total)) if total else Decimal("0")
            term = "long" if _is_long_term(lot.acquired, tx.timestamp) else "short"
            res.disposals.append(Disposal(
                date=tx.timestamp, kind=tx.kind, sats=take,
                proceeds_usd=proceeds_portion.quantize(_CENTS), basis_usd=basis_portion.quantize(_CENTS),
                acquired=lot.acquired, term=term,
            ))
        lot.sats -= take
        lot.basis_usd -= basis_portion
        need -= take
        if lot.sats <= 0:
            del lots[idx]

    if need > 0:
        # Disposed more than we have lots for — missing acquisition history. We record the
        # shortfall as a ZERO-BASIS, SHORT-TERM disposal: zero basis is the conservative IRS
        # treatment (maximizes reported gain) when basis is unsubstantiated, and we can't know
        # the real holding period, so we don't claim long-term. The warning tells the user how
        # to correct it (add an Opening-balance lot with the true acquisition date & cost),
        # which is the right fix — do NOT rely on this fallback for filing.
        if realize:
            proceeds_portion = (proceeds * Decimal(need) / Decimal(total)) if total else Decimal("0")
            res.disposals.append(Disposal(
                date=tx.timestamp, kind=tx.kind, sats=need,
                proceeds_usd=proceeds_portion.quantize(_CENTS), basis_usd=Decimal("0.00"),
                acquired=tx.timestamp, term="short",
            ))
        res.warnings.append(
            f"{TxKind.LABELS.get(tx.kind, tx.kind)} on {tx.timestamp:%Y-%m-%d} exceeds tracked lots by "
            f"{Decimal(need) / SATS_PER_BTC:.8f} BTC — recorded as zero-basis short-term. Add an "
            f"Opening-balance lot (real date & cost) for the missing coins to correct gain & term."
        )

    # Record basis consumed by a transfer_out, so a cross-account transfer can carry it.
    _k = tx_key(tx)
    if not realize and _k:
        res.transfer_out_basis[_k] = res.transfer_out_basis.get(_k, Decimal("0")) + consumed_basis


# --- DB-backed convenience wrappers -----------------------------------------
def _account_method(session: Session, account_id: int) -> str:
    acct = session.get(Account, account_id)
    return acct.lot_method if acct and acct.lot_method in LOT_METHODS else "fifo"


def _account_internal_within(txs: list[Transaction], internal: set[str]) -> set[str]:
    """Txids that move coins between two wallets OF THIS ACCOUNT (both sides present)."""
    here_out = {t.txid for t in txs if t.kind == TxKind.TRANSFER_OUT and t.txid}
    here_in = {t.txid for t in txs if t.kind == TxKind.TRANSFER_IN and t.txid}
    return here_out & here_in & internal


def compute_account(session: Session, account_id: int) -> CostBasisResult:
    return compute(tx_svc.list_transactions(session, account_id), internal_txids(session),
                   method=_account_method(session, account_id))


def compute_wallet(session: Session, account_id: int, wallet_id: int | None) -> CostBasisResult:
    """Per-wallet view. Intra-account moves are detected at the ACCOUNT level (so a transfer
    between two of the account's wallets isn't mistaken for an external disposal/acquisition,
    which would double-count basis). Note: holdings are attributed to the ACQUIRING wallet —
    coins that later moved to a sibling wallet stay counted where their basis originated."""
    all_txs = tx_svc.list_transactions(session, account_id)
    internal = internal_txids(session)
    account_internal = _account_internal_within(all_txs, internal)
    wtxs = [t for t in all_txs if t.wallet_id == wallet_id]
    return compute(wtxs, internal, method=_account_method(session, account_id),
                   account_internal_within=account_internal)


def compute_account_breakdown(
    session: Session, account_id: int
) -> tuple[CostBasisResult, list[tuple[int | None, CostBasisResult]]]:
    """Account result + per-wallet results in a SINGLE ledger load (avoids the N+1 where the
    detail page recomputed the whole account once per wallet). Per-wallet results share the
    account-level internal-transfer set so they stay consistent with the account total."""
    txs = tx_svc.list_transactions(session, account_id)
    internal = internal_txids(session)
    method = _account_method(session, account_id)
    account_internal = _account_internal_within(txs, internal)

    account_res = compute(txs, internal, method=method, account_internal_within=account_internal)
    wallet_ids = sorted({t.wallet_id for t in txs}, key=lambda w: (w is None, w or 0))
    per_wallet = [
        (wid, compute([t for t in txs if t.wallet_id == wid], internal, method=method,
                      account_internal_within=account_internal))
        for wid in wallet_ids
    ]
    return account_res, per_wallet


# Tolerances for matching an exchange withdrawal to its on-chain deposit (no shared txid).
_AMOUNT_TOL_SATS = 100_000      # 0.001 BTC — covers the network fee skimmed in transit
_DATE_WINDOW = dt.timedelta(days=3)


def find_transfer_matches(session: Session) -> list[tuple]:
    """(transfer_out, transfer_in, kind) triples for the SAME owner's coins moving between
    DIFFERENT accounts. kind is "txid" (shared on-chain id — reliable, auto-appliable) or
    "heuristic" (amount+date only — needs human review before mutating basis).

    Owner identity is the tuple (owner_user_id, owner-label): a blank owner label on accounts
    belonging to DIFFERENT app users must NOT compare as the same owner, or one user could
    trigger basis changes on another's books. Matching never crosses owner_user_id."""
    owner_of = {a.id: (a.owner_user_id, _norm_owner(a.owner))
                for a in session.scalars(select(Account)).all()}
    rows = session.scalars(
        select(Transaction).where(Transaction.kind.in_((TxKind.TRANSFER_IN, TxKind.TRANSFER_OUT)))
    ).all()
    outs = sorted([t for t in rows if t.kind == TxKind.TRANSFER_OUT], key=lambda t: (t.timestamp, t.id or 0))
    ins = sorted([t for t in rows if t.kind == TxKind.TRANSFER_IN], key=lambda t: (t.timestamp, t.id or 0))

    def same_owner_cross(o, i):
        return o.account_id != i.account_id and owner_of.get(o.account_id) == owner_of.get(i.account_id)

    pairs, used_in, used_out = [], set(), set()

    # 1) shared-txid matches (most reliable -> safe to auto-apply)
    in_by_txid: dict[str, list] = {}
    for i in ins:
        if i.txid:
            in_by_txid.setdefault(i.txid, []).append(i)
    for o in outs:
        if not o.txid:
            continue
        for i in in_by_txid.get(o.txid, []):
            if i.id not in used_in and same_owner_cross(o, i):
                pairs.append((o, i, "txid")); used_in.add(i.id); used_out.add(o.id); break

    # 2) amount+date matches for the rest (exchange withdrawal -> on-chain deposit). These are
    #    heuristic guesses (a 3-day / 0.001 BTC window can mispair) -> flagged for review, not
    #    auto-applied.
    for o in outs:
        if o.id in used_out:
            continue
        best = None
        for i in ins:
            if i.id in used_in or not same_owner_cross(o, i):
                continue
            if abs(o.amount_sats - i.amount_sats) > _AMOUNT_TOL_SATS:
                continue
            if not (o.timestamp <= i.timestamp <= o.timestamp + _DATE_WINDOW):
                continue
            if best is None or i.timestamp < best.timestamp:
                best = i
        if best is not None:
            pairs.append((o, best, "heuristic")); used_in.add(best.id); used_out.add(o.id)

    pairs.sort(key=lambda p: (p[0].timestamp, p[0].id or 0))
    return pairs


def reclassify_onchain_transfers(session: Session) -> int:
    """Restore the "transfer" label on on-chain (xpub) rows whose counterparty is ANOTHER of
    your loaded wallets — the only case where both sides are visible. A standalone wallet
    imports external moves as buy/sell; when the SAME txid also appears in another same-owner
    wallet (as the opposite direction), it's actually an internal transfer, so relabel both.
    Single-wallet ledgers have no cross-wallet match and keep their buys/sells. Returns the
    number of rows relabeled."""
    owner_of = {a.id: (a.owner_user_id, _norm_owner(a.owner)) for a in session.scalars(select(Account)).all()}
    rows = session.scalars(
        select(Transaction).where(Transaction.source.like("xpub:%"), Transaction.txid.is_not(None))
    ).all()
    by_txid: dict[str, list] = defaultdict(list)
    for t in rows:
        by_txid[t.txid].append(t)

    changed = 0
    for group in by_txid.values():
        outs = [t for t in group if (t.external_id or "").endswith(":out")]
        ins = [t for t in group if (t.external_id or "").endswith(":in")]
        for o in outs:
            for i in ins:
                if o.wallet_id == i.wallet_id:
                    continue
                if owner_of.get(o.account_id) != owner_of.get(i.account_id):
                    continue  # different owner = a gift, not an internal transfer
                if o.kind != TxKind.TRANSFER_OUT:
                    o.kind = TxKind.TRANSFER_OUT
                    changed += 1
                if i.kind != TxKind.TRANSFER_IN:
                    i.kind = TxKind.TRANSFER_IN
                    changed += 1
    if changed:
        session.commit()
    return changed


def reconcile_internal_transfers(session: Session, include_heuristic: bool = False) -> int:
    """Carry cost basis across same-owner self-transfers between accounts. First relabels any
    cross-wallet on-chain buy/sell pairs back to transfers. By default ONLY exact shared-txid
    matches are applied automatically; amount+date heuristic matches are left for explicit
    review (pass include_heuristic=True to apply them too). Honors the per-transfer carry
    opt-out. Returns the number of transfer_ins updated."""
    reclassify_onchain_transfers(session)
    updated = 0
    src_cache: dict[int, CostBasisResult] = {}  # memoize per source account (was recomputed per pair)
    for out_tx, in_tx, kind in find_transfer_matches(session):
        if kind != "txid" and not include_heuristic:
            continue  # don't silently mutate basis on a heuristic guess
        if in_tx.carry_disabled:  # user opted this destination out of carryover
            continue
        if out_tx.account_id not in src_cache:
            src_cache[out_tx.account_id] = compute_account(session, out_tx.account_id)
        consumed = src_cache[out_tx.account_id].transfer_out_basis.get(tx_key(out_tx), Decimal("0"))
        if in_tx.carried_basis_usd != consumed:
            in_tx.carried_basis_usd = consumed
            updated += 1
    if updated:
        session.commit()  # single commit instead of one per updated row
    return updated
