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
import heapq
import json
from dataclasses import dataclass, field
from decimal import Decimal

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SATS_PER_BTC, Account, Transaction, TxKind
from app.services import transactions as tx_svc


def _norm_owner(owner: str | None) -> str:
    return (owner or "").strip().lower()


DISPOSAL_PRIORITIES = ("none", "non_kyc_first", "kyc_first")


def _is_kyc(label: str | None) -> bool:
    """A coin is KYC iff its provenance label normalizes to "kyc". Everything else — an explicit
    "non-KYC", a custom label, or blank/unknown — groups as non-KYC for disposal-priority
    purposes (the conservative choice for the privacy use case: spend non-KYC/unknown first)."""
    return (label or "").strip().lower() == "kyc"


def _merge_kyc(labels) -> str:
    """Collapse several provenance labels into ONE, conservatively: if the inputs span more than
    one distinct non-empty label, the result is "KYC" (a coin commingled from KYC + non-KYC
    inputs is treated as KYC — see docs/utxo-tracking.md). One label ⇒ itself; none ⇒ "".
    Used where coins MUST carry a single label (a future UTXO-consolidation output); the
    fragment-rebuild carry keeps fragments separate, so it isn't needed on the normal path."""
    distinct = {(l or "").strip() for l in labels if (l or "").strip()}
    if len(distinct) > 1:
        return "KYC"
    return next(iter(distinct)) if distinct else ""


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


def _fragments_json(frags: list[dict] | None) -> str | None:
    """Serialize a transfer_out's consumed-lot fragments (from transfer_out_lots) into the JSON
    stored on the destination transfer_in's `carried_lots`, so compute() can rebuild the lots.
    datetimes → ISO, Decimals → str. None/empty ⇒ None (fall back to the single carried lot)."""
    if not frags:
        return None
    return json.dumps([
        {"acquired": f["acquired"].isoformat(), "sats": int(f["sats"]),
         "basis": str(f["basis"]), "kyc": f.get("kyc", "")}
        for f in frags
    ])


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
    kyc_origin: str = ""  # provenance label ("KYC"/"non-KYC"/…); "" = unknown


@dataclass
class Disposal:
    date: dt.datetime
    kind: str
    sats: int
    proceeds_usd: Decimal
    basis_usd: Decimal
    acquired: dt.datetime
    term: str  # "short" | "long"
    kyc_origin: str = ""  # provenance of the lot this fragment consumed

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

    @property
    def holding_by_kyc(self) -> dict[str, dict]:
        """Open-lot holdings bucketed by provenance label: {label: {"sats", "basis_usd"}}.
        Key "" means unknown (e.g. an unreconciled transfer in). Buckets are the raw labels, so
        they break down naturally with no forced merge (fragment-rebuild keeps lots separate)."""
        out: dict[str, dict] = {}
        for lot in self.open_lots:
            b = out.setdefault(lot.kyc_origin or "", {"sats": 0, "basis_usd": Decimal("0")})
            b["sats"] += lot.sats
            b["basis_usd"] += lot.basis_usd
        for b in out.values():
            b["basis_usd"] = b["basis_usd"].quantize(_CENTS)
        return out

    @property
    def realized_by_kyc(self) -> dict[str, dict]:
        """Realized gain bucketed by the consumed lot's provenance: {label: {short, long, total}}."""
        out: dict[str, dict] = {}
        for d in self.disposals:
            b = out.setdefault(d.kyc_origin or "",
                               {"short": Decimal("0"), "long": Decimal("0"), "total": Decimal("0")})
            b[d.term] += d.gain_usd
            b["total"] += d.gain_usd
        for b in out.values():
            for k in b:
                b[k] = b[k].quantize(_CENTS)
        return out


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


def _method_index(lots: list[Lot], candidates: list[int], method: str) -> int:
    """Pick the next lot from `candidates` (indices into `lots`) by the within-class ordering."""
    if method == "lifo":
        return candidates[-1]
    if method == "hifo":  # highest cost basis per sat first (minimizes gain)
        best_rate, best_i = None, candidates[0]
        for i in candidates:
            lot = lots[i]
            rate = (lot.basis_usd / lot.sats) if lot.sats else Decimal("0")
            if best_rate is None or rate > best_rate:
                best_rate, best_i = rate, i
        return best_i
    return candidates[0]  # fifo — lots are kept in acquisition (append) order


def _select_index(lots: list[Lot], method: str, priority: str = "none") -> int:
    """Index of the lot to consume next. With a KYC `priority`, the preferred class is exhausted
    first (specific-ID by class); within the chosen pool, ordering follows `method`."""
    if priority in ("non_kyc_first", "kyc_first"):
        prefer_kyc = priority == "kyc_first"
        pref = [i for i, lot in enumerate(lots) if _is_kyc(lot.kyc_origin) == prefer_kyc]
        candidates = pref if pref else list(range(len(lots)))
    else:
        candidates = list(range(len(lots)))
    return _method_index(lots, candidates, method)


def _carried_fragment_lots(tx: Transaction) -> list[Lot] | None:
    """Rebuild a transfer_in's destination lots from the source fragments the reconciler stored
    in `carried_lots`. Each fragment keeps its ORIGINAL acquisition date (so the holding period
    tacks across a self-transfer, IRC §1223) and its own KYC label. Returns None when there are
    no usable fragments (caller falls back to the single carried_basis_usd lot) or the user opted
    this transfer out of carryover."""
    if tx.kind != TxKind.TRANSFER_IN or tx.carry_disabled or not tx.carried_lots:
        return None
    try:
        frags = json.loads(tx.carried_lots)
    except (ValueError, TypeError):
        return None
    parsed = []
    for f in sorted(frags, key=lambda fr: fr.get("acquired", "")):
        sats = int(f.get("sats", 0))
        if sats <= 0:
            continue
        parsed.append((dt.datetime.fromisoformat(f["acquired"]), sats,
                       Decimal(str(f.get("basis", "0"))), f.get("kyc", "") or ""))
    if not parsed:
        return None

    # The fragments sum to the SOURCE outflow; the destination may have received slightly less
    # (a network fee skimmed in transit on a heuristic amount+date match). Scale the sats to what
    # was actually received while preserving each fragment's basis (the fee doesn't change basis),
    # so holdings stay exact instead of overstated. Shared-txid matches have equal amounts → no
    # scaling, byte-identical to the unscaled fragments.
    frag_total = sum(p[1] for p in parsed)
    target = tx.amount_sats
    scale = target > 0 and frag_total > 0 and target != frag_total
    out: list[Lot] = []
    used = 0
    pending_basis = Decimal("0")  # basis from any fragment that scaled to 0 sats — never dropped
    for i, (acquired, sats, basis, kyc) in enumerate(parsed):
        n_sats = sats if not scale else (target - used if i == len(parsed) - 1 else sats * target // frag_total)
        if n_sats <= 0:
            pending_basis += basis
            continue
        out.append(Lot(acquired=acquired, sats=int(n_sats), basis_usd=basis + pending_basis,
                       source=tx.source, kyc_origin=kyc))
        pending_basis = Decimal("0")
        used += int(n_sats)
    if pending_basis and out:  # trailing fragment(s) scaled out — fold their basis into the last lot
        out[-1].basis_usd += pending_basis
    return out or None


def compute(txs: list[Transaction], internal_txids: set[str] | None = None,
            method: str = "fifo", account_internal_within: set[str] | None = None,
            priority: str = "none") -> CostBasisResult:
    """Run the lot engine over `txs`.

    `account_internal_within` is the set of txids that are internal moves between wallets of
    the SAME account; those are skipped (no basis change). It MUST be computed at the account
    level: when computing a single wallet's view, the wallet's own tx subset only contains one
    side of an intra-account move, so deriving it from `txs` here would miss it and the
    per-wallet basis would double-count. Callers computing a whole account can leave it None
    (we derive it from `txs`, which then contains both sides).

    `priority` is the account's KYC disposal preference (none/non_kyc_first/kyc_first). With the
    default "none" the engine is byte-identical to before, including the HIFO max-heap fast path;
    a KYC priority falls back to a linear class-aware selection (opt-in).
    """
    res = CostBasisResult()
    lots: list[Lot] = []
    hifo_heap: list[tuple[Decimal, int, Lot]] = []
    method = method if method in LOT_METHODS else "fifo"
    priority = priority if priority in DISPOSAL_PRIORITIES else "none"
    # The HIFO heap is an ordering optimization; a KYC priority reorders selection, so use it
    # only on the default path. (Selection then goes through the linear _select_index.)
    use_heap = method == "hifo" and priority == "none"
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
            # A reconciled transfer_in rebuilds MULTIPLE lots from the source fragments, keeping
            # each one's original acquisition date (holding period tacks) and its own KYC label.
            frag_lots = _carried_fragment_lots(tx)
            if frag_lots is not None:
                for lot in frag_lots:
                    lots.append(lot)
                    if use_heap:
                        rate = (lot.basis_usd / lot.sats) if lot.sats else Decimal("0")
                        heapq.heappush(hifo_heap, (-rate, len(lots), lot))
                continue

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
                lot = Lot(acquired=tx.timestamp, sats=tx.amount_sats, basis_usd=basis,
                          source=tx.source, kyc_origin=tx.kyc_origin or "")
                lots.append(lot)
                if use_heap:
                    rate = (basis / tx.amount_sats) if tx.amount_sats else Decimal("0")
                    heapq.heappush(hifo_heap, (-rate, len(lots), lot))

        elif tx.kind in (TxKind.SELL, TxKind.SPEND):
            proceeds = (tx.fiat_value or Decimal("0"))
            if tx.kind == TxKind.SELL and tx.fiat_fee:
                proceeds -= tx.fiat_fee
            _dispose(lots, tx, proceeds, res, realize=True, method=method,
                     hifo_heap=hifo_heap if use_heap else None, priority=priority)

        elif tx.kind == TxKind.TRANSFER_OUT:
            _dispose(lots, tx, Decimal("0"), res, realize=False, method=method,
                     hifo_heap=hifo_heap if use_heap else None, priority=priority)
        # FEE: ignored for basis

    res.open_lots = [lot for lot in lots if lot.sats > 0]
    return res


def _dispose(lots: list[Lot], tx: Transaction, proceeds: Decimal, res: CostBasisResult,
             realize: bool, method: str = "fifo",
             hifo_heap: list[tuple[Decimal, int, Lot]] | None = None,
             priority: str = "none") -> None:
    need = tx.amount_sats
    total = need
    consumed_basis = Decimal("0")
    while need > 0 and lots:
        idx = None
        if hifo_heap is not None:  # default HIFO fast path (priority == "none"; see compute)
            while hifo_heap and hifo_heap[0][2].sats <= 0:
                heapq.heappop(hifo_heap)
            if not hifo_heap:
                break
            lot = hifo_heap[0][2]
        else:
            idx = _select_index(lots, method, priority)
            lot = lots[idx]
        take = min(lot.sats, need)
        basis_portion = (lot.basis_usd * Decimal(take) / Decimal(lot.sats)) if lot.sats else Decimal("0")
        consumed_basis += basis_portion
        _k = tx_key(tx)
        if not realize and _k:
            res.transfer_out_lots.setdefault(_k, []).append(
                {"acquired": lot.acquired, "sats": take, "basis": basis_portion.quantize(_CENTS),
                 "kyc": lot.kyc_origin}
            )
        if realize:
            proceeds_portion = (proceeds * Decimal(take) / Decimal(total)) if total else Decimal("0")
            term = "long" if _is_long_term(lot.acquired, tx.timestamp) else "short"
            res.disposals.append(Disposal(
                date=tx.timestamp, kind=tx.kind, sats=take,
                proceeds_usd=proceeds_portion.quantize(_CENTS), basis_usd=basis_portion.quantize(_CENTS),
                acquired=lot.acquired, term=term, kyc_origin=lot.kyc_origin,
            ))
        lot.sats -= take
        lot.basis_usd -= basis_portion
        need -= take
        if lot.sats <= 0:
            if hifo_heap is not None:
                heapq.heappop(hifo_heap)
            elif idx is not None:
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


def _account_priority(session: Session, account_id: int) -> str:
    acct = session.get(Account, account_id)
    pr = getattr(acct, "disposal_priority", "none")
    return pr if pr in DISPOSAL_PRIORITIES else "none"


def _account_internal_within(txs: list[Transaction], internal: set[str]) -> set[str]:
    """Txids that move coins between two wallets OF THIS ACCOUNT (both sides present)."""
    here_out = {t.txid for t in txs if t.kind == TxKind.TRANSFER_OUT and t.txid}
    here_in = {t.txid for t in txs if t.kind == TxKind.TRANSFER_IN and t.txid}
    return here_out & here_in & internal


def compute_account(session: Session, account_id: int) -> CostBasisResult:
    return compute(tx_svc.list_transactions(session, account_id), internal_txids(session),
                   method=_account_method(session, account_id),
                   priority=_account_priority(session, account_id))


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
                   account_internal_within=account_internal,
                   priority=_account_priority(session, account_id))


def compute_account_breakdown(
    session: Session, account_id: int
) -> tuple[CostBasisResult, list[tuple[int | None, CostBasisResult]]]:
    """Account result + per-wallet results in a SINGLE ledger load (avoids the N+1 where the
    detail page recomputed the whole account once per wallet). Per-wallet results share the
    account-level internal-transfer set so they stay consistent with the account total."""
    txs = tx_svc.list_transactions(session, account_id)
    internal = internal_txids(session)
    method = _account_method(session, account_id)
    priority = _account_priority(session, account_id)
    account_internal = _account_internal_within(txs, internal)

    account_res = compute(txs, internal, method=method, account_internal_within=account_internal,
                          priority=priority)
    wallet_ids = sorted({t.wallet_id for t in txs}, key=lambda w: (w is None, w or 0))
    per_wallet = [
        (wid, compute([t for t in txs if t.wallet_id == wid], internal, method=method,
                      account_internal_within=account_internal, priority=priority))
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

    Owner identity is the account's `owner` label (blank = you): a transfer to a DIFFERENT
    owner is a gift, not a self-transfer, so basis must not carry across it."""
    owner_of = {a.id: _norm_owner(a.owner)
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
    """The "unless connected" half of the app-wide buy/sell-by-default rule: connect coins moving
    between two of your OWN wallets and relabel both sides as a transfer.

    Importers (and standalone xpub mode) record an ambiguous move as a TAXABLE buy/sell. Here, any
    on-chain txid that appears as an outflow (sell/transfer_out) in one wallet AND an inflow
    (buy/transfer_in) in ANOTHER wallet of the SAME owner is an internal self-transfer — relabel
    both to transfer_out/transfer_in (so no phantom gain, and basis carries via the reconciler).
    Source-agnostic: it connects a Strike/Swan CSV disposal to the xpub wallet that received it,
    not just xpub-to-xpub. A move to a DIFFERENT owner is a gift (left as buy/sell). An outflow
    with no matching same-owner inflow stays a sell. Returns the number of rows relabeled."""
    owner_of = {a.id: _norm_owner(a.owner) for a in session.scalars(select(Account)).all()}
    rows = session.scalars(select(Transaction).where(
        Transaction.txid.is_not(None),
        Transaction.kind.in_((TxKind.BUY, TxKind.SELL, TxKind.TRANSFER_IN, TxKind.TRANSFER_OUT)),
    )).all()
    by_txid: dict[str, list] = defaultdict(list)
    for t in rows:
        by_txid[t.txid].append(t)

    changed = 0
    for group in by_txid.values():
        outs = [t for t in group if t.kind in (TxKind.SELL, TxKind.TRANSFER_OUT)]
        ins = [t for t in group if t.kind in (TxKind.BUY, TxKind.TRANSFER_IN)]
        for o in outs:
            for i in ins:
                if (o.account_id, o.wallet_id) == (i.account_id, i.wallet_id):
                    continue  # same wallet — not a cross-wallet transfer
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


# --- Reconciliation inbox ----------------------------------------------------
# Candidate self-transfers that share NO txid (coins that left one wallet and reappeared in
# another through an address we don't track). These are SUGGESTIONS only — never auto-applied,
# because on-chain we can't prove the intermediary is yours (see docs/utxo-tracking.md scope).
_SUGGEST_WINDOW = dt.timedelta(days=7)          # an inflow within a week of the outflow
_SUGGEST_AMOUNT_TOL_SATS = 200_000              # 0.002 BTC — a couple of hops' worth of fees
_OUTFLOW_SUGGEST = (TxKind.SELL, TxKind.TRANSFER_OUT)
_INFLOW_SUGGEST = (TxKind.BUY, TxKind.TRANSFER_IN)


@dataclass
class TransferSuggestion:
    out_tx: Transaction
    in_tx: Transaction
    amount_delta_sats: int
    days_apart: int
    out_account: str = ""
    in_account: str = ""

    @property
    def confidence(self) -> str:
        if self.amount_delta_sats <= 10_000 and self.days_apart <= 1:
            return "high"
        if self.amount_delta_sats <= 100_000 and self.days_apart <= 3:
            return "medium"
        return "low"


def suggest_transfers(session: Session) -> list[TransferSuggestion]:
    """Propose same-owner outflow→inflow pairs that look like one self-transfer split across two
    transactions with different txids. High precision over recall: one best inflow per outflow,
    inside a tight amount+time window, excluding anything already proven (shared txid) or already
    adjudicated (transfer_reviewed) or already carrying basis. The user confirms or rejects each.
    """
    accts = {a.id: a for a in session.scalars(select(Account)).all()}
    owner_of = {aid: _norm_owner(a.owner) for aid, a in accts.items()}
    internal = internal_txids(session)
    rows = session.scalars(
        select(Transaction).where(Transaction.kind.in_(_OUTFLOW_SUGGEST + _INFLOW_SUGGEST))
    ).all()

    def eligible(t: Transaction) -> bool:
        return not t.transfer_reviewed and not (t.txid and t.txid in internal)

    outs = sorted([t for t in rows if t.kind in _OUTFLOW_SUGGEST and eligible(t)],
                  key=lambda t: (t.timestamp, t.id or 0))
    ins = [t for t in rows if t.kind in _INFLOW_SUGGEST and eligible(t) and t.carried_basis_usd is None]

    used_in: set[int] = set()
    out: list[TransferSuggestion] = []
    for o in outs:
        best, best_score = None, None
        for i in ins:
            if i.id in used_in or (o.account_id, o.wallet_id) == (i.account_id, i.wallet_id):
                continue
            if owner_of.get(o.account_id) != owner_of.get(i.account_id):
                continue
            if o.txid and i.txid and o.txid == i.txid:
                continue  # shared txid -> handled by the auto reconciler, not a suggestion
            if not (o.timestamp <= i.timestamp <= o.timestamp + _SUGGEST_WINDOW):
                continue
            delta = abs(o.amount_sats - i.amount_sats)
            if delta > _SUGGEST_AMOUNT_TOL_SATS:
                continue
            score = (delta, i.timestamp - o.timestamp)
            if best_score is None or score < best_score:
                best, best_score = i, score
        if best is not None:
            used_in.add(best.id)
            out.append(TransferSuggestion(
                out_tx=o, in_tx=best, amount_delta_sats=abs(o.amount_sats - best.amount_sats),
                days_apart=(best.timestamp - o.timestamp).days,
                out_account=accts[o.account_id].name, in_account=accts[best.account_id].name))
    out.sort(key=lambda s: (s.out_tx.timestamp, s.out_tx.id or 0))
    return out


def _same_owner(session: Session, a_id: int, b_id: int) -> bool:
    a, b = session.get(Account, a_id), session.get(Account, b_id)
    if a is None or b is None:
        return False
    return _norm_owner(a.owner) == _norm_owner(b.owner)


def confirm_transfer(session: Session, out_tx_id: int, in_tx_id: int) -> tuple[bool, str]:
    """Confirm a suggested pair: relabel both rows to transfer_out/transfer_in and carry the
    source lot's basis onto the destination. Marks both reviewed so they leave the queue."""
    o, i = session.get(Transaction, out_tx_id), session.get(Transaction, in_tx_id)
    if o is None or i is None:
        return False, "transaction not found"
    if o.kind not in _OUTFLOW_SUGGEST or i.kind not in _INFLOW_SUGGEST:
        return False, "not an outflow/inflow pair"
    if not _same_owner(session, o.account_id, i.account_id):
        return False, "different owners — that's a gift/disposal, not a self-transfer"
    o.kind, i.kind = TxKind.TRANSFER_OUT, TxKind.TRANSFER_IN
    i.carry_disabled = False
    o.transfer_reviewed = i.transfer_reviewed = True
    session.commit()
    # With the source row now a transfer_out, compute records the basis + lot fragments it
    # consumed; carry both (fragments preserve original acquisition dates + KYC labels).
    src = compute_account(session, o.account_id)
    key = tx_key(o)
    i.carried_basis_usd = src.transfer_out_basis.get(key, Decimal("0"))
    i.carried_lots = _fragments_json(src.transfer_out_lots.get(key, []))
    session.commit()
    return True, ""


def reject_suggestion(session: Session, out_tx_id: int, in_tx_id: int) -> tuple[bool, str]:
    """Reject a suggested pair: the rows are genuine external buy/sell, not a self-transfer.
    Marks both reviewed so the pairing isn't proposed again."""
    o, i = session.get(Transaction, out_tx_id), session.get(Transaction, in_tx_id)
    if o is None or i is None:
        return False, "transaction not found"
    o.transfer_reviewed = i.transfer_reviewed = True
    session.commit()
    return True, ""


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
        src = src_cache[out_tx.account_id]
        key = tx_key(out_tx)
        consumed = src.transfer_out_basis.get(key, Decimal("0"))
        frags_json = _fragments_json(src.transfer_out_lots.get(key, []))
        if in_tx.carried_basis_usd != consumed or in_tx.carried_lots != frags_json:
            in_tx.carried_basis_usd = consumed
            in_tx.carried_lots = frags_json
            updated += 1
    if updated:
        session.commit()  # single commit instead of one per updated row
    return updated
