# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""CSV importers for custodial sources + a canonical generic format.

Each parser maps source-specific rows to NormalizedTx records. Header matching is
case-insensitive and tolerant. Real-world exports vary — verify mappings against a
redacted sample from each source before trusting them (see docs/importers.md).
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models import SATS_PER_BTC, TxKind
from app.services import transactions as tx_svc


@dataclass
class NormalizedTx:
    kind: str
    timestamp: dt.datetime | None
    amount_sats: int
    fiat_value: Decimal | None = None
    fee_sats: int = 0
    fiat_fee: Decimal | None = None
    price_usd: Decimal | None = None
    txid: str | None = None
    address: str | None = None
    counterparty: str = ""
    external_id: str | None = None
    note: str = ""
    # Custodian-provided carryover data for a transferred-in coin (e.g. Swan): the original cost
    # basis (USD) and acquisition date. When present on a TRANSFER_IN, they become the lot's basis
    # and holding-period origin (see persist_records / costbasis).
    cost_basis_usd: Decimal | None = None
    acquired_at: dt.datetime | None = None
    # The full original (normalized) CSV row — stashed verbatim so nothing the export offered is
    # lost, surfaced in the transaction detail view.
    raw: dict = field(default_factory=dict)


@dataclass
class ImportResult:
    imported: int = 0
    skipped: int = 0                                       # exact duplicates (idempotent re-import)
    errors: list[str] = field(default_factory=list)        # whole-file failures
    rejected: list[str] = field(default_factory=list)      # per-row issues (bad date/amount, ignored rows)


# --- helpers -----------------------------------------------------------------
def _norm_keys(row: dict) -> dict:
    return {(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}


def _get(row: dict, *names: str) -> str:
    for n in names:
        v = row.get(n.lower())
        if v not in (None, ""):
            return str(v)
    return ""


def _to_sats(s: str) -> int:
    if not s:
        return 0
    s = s.replace(",", "").replace("BTC", "").replace("₿", "").strip()
    try:
        return int((Decimal(s).copy_abs() * SATS_PER_BTC).to_integral_value(rounding="ROUND_HALF_UP"))
    except (InvalidOperation, ValueError):
        return 0


def _usd(s: str) -> Decimal | None:
    if not s:
        return None
    # Strip currency noise and accounting-style parens (Coinbase writes negatives as "($84.63)").
    s = (s.replace("$", "").replace(",", "").replace("USD", "")
         .replace("(", "").replace(")", "").strip())
    if s in ("", "-"):
        return None
    try:
        return Decimal(s).copy_abs().quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def to_naive_utc(d: dt.datetime | None) -> dt.datetime | None:
    """Normalize a timestamp to naive UTC. Timezone-aware values are CONVERTED to UTC (not just
    stripped) — dropping a +05:00 offset would shift the tax date/year and the historical price
    hour. Naive values are assumed to already be UTC (the app's storage convention)."""
    if d is None:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(dt.UTC)
    return d.replace(tzinfo=None)


def _dt(s: str) -> dt.datetime | None:
    """Parse a timestamp to naive UTC, or None if unrecognized. Returning None (rather than a
    1970 sentinel) lets the importer REJECT the row with a reason instead of silently recording
    a wrong date that would land in the wrong tax year."""
    s = (s or "").strip()
    # ISO-8601 first so any timezone offset is parsed and converted to UTC.
    try:
        return to_naive_utc(dt.datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        pass
    bare = s.replace("Z", "").strip()
    for suffix in (" UTC", " GMT"):           # Coinbase: "2022-03-02 11:23:36 UTC"
        if bare.endswith(suffix):
            bare = bare[: -len(suffix)].strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d",
                "%b %d %Y %H:%M:%S", "%b %d %Y", "%d %b %Y %H:%M:%S"):  # Strike: "Oct 10 2022 22:41:09"
        try:
            return dt.datetime.strptime(bare, fmt)  # naive => already UTC by convention
        except ValueError:
            continue
    return None


def _stable_id(source: str, n: NormalizedTx) -> str:
    raw = f"{source}|{n.timestamp.isoformat()}|{n.kind}|{n.amount_sats}|{n.fiat_value}|{n.txid or ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]


# Column names that mark the real header row. Some exports (e.g. Swan) prefix the file with
# banner lines (company name, phone) before the header; without skipping them csv.DictReader
# treats the banner as the field names and drops every data row.
_HEADER_VOCAB = {
    "event", "type", "kind", "date", "timestamp", "time", "time (utc)", "created at",
    "executed at", "transaction type", "transaction id", "bitcoin amount", "btc amount",
    "unit count", "amount", "asset", "asset type", "quantity transacted",
}


def _strip_preamble(text: str) -> str:
    """Drop leading banner lines, returning the text from the real header row onward. The header
    is the first line with >=2 recognized column names — for a normal export that's line 0, so
    this is a no-op; for a Swan export it skips the company/phone banner."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        try:
            cells = next(csv.reader([line]))
        except (StopIteration, csv.Error):
            continue
        if sum(1 for c in cells if (c or "").strip().lower() in _HEADER_VOCAB) >= 2:
            return "".join(lines[i:])
    return text


# --- kind maps ---------------------------------------------------------------
# App-wide rule: a BTC movement defaults to a TAXABLE buy/sell unless it can be connected to
# another of your own wallets (shared on-chain txid), which upgrades it to a transfer during
# reconciliation. So importers never emit transfers from ambiguous movement terms.
#
# Generic is the user-controlled canonical format, so it ALSO honors an EXPLICIT transfer_in/
# transfer_out (the user asserting a self-transfer); only the ambiguous aliases
# (deposit/receive/withdrawal/send) fall back to buy/sell.
_GENERIC_KIND = {
    "buy": TxKind.BUY, "purchase": TxKind.BUY, "sell": TxKind.SELL, "sale": TxKind.SELL,
    "income": TxKind.INCOME, "reward": TxKind.INCOME, "rewards": TxKind.INCOME, "interest": TxKind.INCOME,
    "spend": TxKind.SPEND, "payment": TxKind.SPEND,
    "transfer_in": TxKind.TRANSFER_IN, "transfer_out": TxKind.TRANSFER_OUT,   # explicit -> honored
    "deposit": TxKind.BUY, "receive": TxKind.BUY,                             # ambiguous -> buy/sell
    "withdrawal": TxKind.SELL, "send": TxKind.SELL, "withdraw": TxKind.SELL,
    "fee": TxKind.FEE,
}

# Custodial-source default (Coinbase/Strike/Swan/Bisq): EVERY movement is a buy/sell — a custodial
# export can't assert a self-transfer, so even "transfer_in"/"transfer_out" default to buy/sell.
# The reconciler turns them into transfers when a shared txid connects two of your wallets.
_CUSTODIAL_KIND = {
    "buy": TxKind.BUY, "purchase": TxKind.BUY, "deposit": TxKind.BUY, "receive": TxKind.BUY,
    "transfer_in": TxKind.BUY,
    "sell": TxKind.SELL, "sale": TxKind.SELL, "withdrawal": TxKind.SELL, "withdraw": TxKind.SELL,
    "send": TxKind.SELL, "transfer_out": TxKind.SELL,
    "spend": TxKind.SPEND, "payment": TxKind.SPEND,
    "income": TxKind.INCOME, "reward": TxKind.INCOME, "rewards": TxKind.INCOME, "interest": TxKind.INCOME,
}

_COINBASE_KIND = {
    "buy": TxKind.BUY, "advanced trade buy": TxKind.BUY, "advance trade buy": TxKind.BUY,
    "sell": TxKind.SELL, "advanced trade sell": TxKind.SELL,
    "receive": TxKind.BUY, "send": TxKind.SELL,   # default to buy/sell; reconciler upgrades to transfer
    "rewards income": TxKind.INCOME, "reward income": TxKind.INCOME, "staking income": TxKind.INCOME,
    "learning reward": TxKind.INCOME, "coinbase earn": TxKind.INCOME, "inflation reward": TxKind.INCOME,
    # "convert" / "pro withdrawal" / "pro deposit" are direction-ambiguous -> resolved by the
    # BTC quantity sign in parse_coinbase, not mapped here.
}


def _map_kind(table: dict, raw: str) -> str | None:
    return table.get((raw or "").strip().lower())


# --- parsers -----------------------------------------------------------------
# Each parser receives rows whose keys/values are already normalized by import_csv (lowercased,
# stripped via _norm_keys), so they index columns directly with _get(r, ...).
def parse_generic(rows: list[dict]) -> list[NormalizedTx]:
    out = []
    for r in rows:
        kind = _map_kind(_GENERIC_KIND, _get(r, "kind", "type", "event", "transaction type"))
        if not kind:
            continue
        out.append(NormalizedTx(
            kind=kind,
            timestamp=_dt(_get(r, "date", "timestamp", "time", "time (utc)")),
            amount_sats=_to_sats(_get(r, "amount_btc", "btc", "btc amount", "amount", "quantity")),
            fiat_value=_usd(_get(r, "usd_value", "usd", "usd amount", "value", "fiat", "total")),
            fee_sats=_to_sats(_get(r, "fee_btc", "fee btc")),
            fiat_fee=_usd(_get(r, "fee_usd", "fee", "fees")),
            txid=_get(r, "txid", "tx", "transaction id", "hash") or None,
            address=_get(r, "address", "destination", "to") or None,
            cost_basis_usd=_usd(_get(r, "cost_basis", "usd_cost_basis", "cost basis")),
            acquired_at=_dt(_get(r, "acquired", "acquired_at", "acquisition_date")),
            counterparty=_get(r, "counterparty", "source", "exchange"),
            external_id=_get(r, "external_id", "id", "reference") or None,
            note=_get(r, "note", "notes", "memo"),
            raw=dict(r),
        ))
    return out


def parse_coinbase(rows: list[dict]) -> list[NormalizedTx]:
    """Coinbase "Transaction history" export. The 3-line preamble (Transactions / User,name,id)
    is skipped by _strip_preamble. Timestamps carry a " UTC" suffix; negatives are accounting-
    style "($84.63)". Only BTC rows are kept."""
    out = []
    for r in rows:
        asset = _get(r, "asset", "currency").upper()
        if asset and asset != "BTC":
            continue
        raw_type = _get(r, "transaction type", "type")
        qty = _get(r, "quantity transacted", "quantity", "amount")
        kind = _map_kind(_COINBASE_KIND, raw_type)
        # Convert (BTC<->stablecoin), Pro deposits/withdrawals, and any unmapped movement resolve
        # to buy/sell by the BTC quantity SIGN (negative = disposal, positive = acquisition) — the
        # app-wide default. A USDC->BTC convert is a BUY; BTC->USDC a SELL.
        if kind is None or raw_type.strip().lower() in ("convert", "pro withdrawal", "pro deposit"):
            kind = TxKind.SELL if qty.strip().startswith("-") else TxKind.BUY
        out.append(NormalizedTx(
            kind=kind,
            timestamp=_dt(_get(r, "timestamp", "date")),
            amount_sats=_to_sats(qty),
            # Total is INCLUSIVE of fees/spread — it IS the basis (buy) / net proceeds (sell)
            # directly, so don't also pass a separate fee (that would double-count it).
            fiat_value=_usd(_get(r, "total (inclusive of fees and/or spread)", "total", "subtotal")),
            price_usd=_usd(_get(r, "price at transaction", "spot price at transaction", "spot price", "price")),
            counterparty="Coinbase",
            note=_get(r, "notes", "note"),
            raw=dict(r),
        ))
    return out


def parse_strike(rows: list[dict]) -> list[NormalizedTx]:
    """Strike Annual Account Statement: Transaction ID, Time (UTC), Status, Transaction Type,
    Amount USD, Fee USD, Amount BTC, Fee BTC, Description, Exchange Rate, Transaction Hash.

    Strike is a dual USD+BTC account. A row with NO Amount BTC is USD-account activity, not a
    BTC tax event: fiat Deposit/Withdrawal (bank funding the USD balance), or a USD spend that
    Strike instantly converts to BTC to settle a Lightning/on-chain invoice. In that conversion
    the BTC is acquired and disposed in the same instant at the same price — never held — so
    there is no disposal of held BTC and ~zero gain. We therefore skip rows with no BTC amount:
    that's CORRECT treatment, not a data workaround. (Do NOT "fix" this by deriving a BTC size
    from the USD — that would fabricate taxable disposals the user never had.) Only rows that
    touch held BTC matter: Purchase (basis) and BTC-denominated Send (BTC leaving the stack).
    Pending/failed rows are skipped too. (The older idealized
    `Time (UTC),Transaction Type,Amount BTC,Amount USD,BTC Price,Fee,Reference` header with a BTC
    amount on every row still imports unchanged.)

    BTC rows default to a TAXABLE buy/sell (Purchase/Receive → buy, Sale/Send → sell), never a
    transfer — coins leaving to / arriving from an unknown destination are a disposal/acquisition
    until the user connects them to one of their own wallets. Bill-pay (pay a USD bill with BTC)
    is a Sale (the BTC disposal, kept) + a Withdrawal (the USD to the biller, skipped)."""
    out = []
    for r in rows:
        status = _get(r, "status").lower()
        if status and status not in ("completed", "complete", "settled"):
            continue
        kind = _map_kind(_CUSTODIAL_KIND, _get(r, "transaction type", "type", "event"))
        if not kind:
            continue
        btc = _get(r, "amount btc", "amount (btc)", "btc", "amount")
        if _to_sats(btc) == 0:                          # USD-only (fiat funding / Lightning) -> not a BTC event
            continue
        out.append(NormalizedTx(
            kind=kind,
            timestamp=_dt(_get(r, "time (utc)", "time", "date", "timestamp")),
            amount_sats=_to_sats(btc),
            fiat_value=_usd(_get(r, "amount usd", "usd", "amount (usd)", "value")),
            fee_sats=_to_sats(_get(r, "fee btc")),
            fiat_fee=_usd(_get(r, "fee usd", "fee", "fees")),
            price_usd=_usd(_get(r, "exchange rate", "btc price", "price")),
            txid=_get(r, "transaction hash", "txid", "on-chain txid") or None,
            address=_get(r, "destination", "destination address", "address") or None,
            counterparty="Strike",
            external_id=_get(r, "transaction id", "reference", "id") or None,
            raw=dict(r),
        ))
    return out


def parse_swan(rows: list[dict]) -> list[NormalizedTx]:
    """Swan ships two unrelated exports. The transactions/transfers export has an `Event`
    column; the on-chain withdrawals export has none (identify it by its own columns)."""
    if not rows:
        return []
    cols = set(rows[0])  # keys already normalized (lowercased/stripped) by import_csv
    if "bitcoin amount" in cols and "created at" in cols:
        return _parse_swan_withdrawals(rows)
    return _parse_swan_transactions(rows)


def _parse_swan_transactions(rows: list[dict]) -> list[NormalizedTx]:
    """Swan's transactions export: Event, Date, ..., Unit Count, Asset Type, BTC Price, ...
    It interleaves BTC rows with USD rows (fiat funding deposits, monthly fees) — only the
    BTC-asset rows are ledger events, so non-BTC rows are filtered out. (The older idealized
    `BTC Amount`/`USD Amount` header with no Asset Type column still works: an absent Asset
    Type is treated as BTC, and the amount/value fall through to the legacy column names.)
    BTC rows default to buy/sell (purchase/deposit -> buy); the reconciler upgrades a row to a
    transfer when a shared txid connects it to one of your own wallets."""
    out = []
    for r in rows:
        asset = _get(r, "asset type", "asset")
        if asset and asset.upper() != "BTC":          # USD funding / fees -> not a BTC event
            continue
        event = _get(r, "event", "type", "transaction type")
        kind = _map_kind(_CUSTODIAL_KIND, event)
        if not kind:
            continue
        cost_basis = _usd(_get(r, "usd cost basis", "cost basis"))
        acq = _dt(_get(r, "acquisition date"))
        # A BTC inflow Swan tags with an original cost basis + acquisition date is a transfer-IN
        # of coins you already owned (not a buy at deposit time) — honor the carryover instead of
        # fabricating a buy. (Deliberate exception to the custodial deposit->buy default.)
        if kind == TxKind.BUY and event.strip().lower() in ("deposit", "receive", "transfer_in") \
                and (cost_basis is not None or acq is not None):
            kind = TxKind.TRANSFER_IN
        out.append(NormalizedTx(
            kind=kind,
            timestamp=_dt(_get(r, "date", "timestamp", "time")),
            amount_sats=_to_sats(_get(r, "unit count", "btc amount", "amount btc", "amount", "btc")),
            # A transfer-in carries basis (below), not a fiat_value at receipt.
            fiat_value=(None if kind == TxKind.TRANSFER_IN
                        else _usd(_get(r, "transaction usd", "total usd", "usd amount", "usd", "value"))),
            fiat_fee=_usd(_get(r, "fee usd", "fee")),
            price_usd=_usd(_get(r, "btc price", "price")),
            cost_basis_usd=cost_basis,
            acquired_at=acq,
            counterparty="Swan",
            external_id=_get(r, "transaction id", "id", "reference") or None,
            raw=dict(r),
        ))
    return out


def _parse_swan_withdrawals(rows: list[dict]) -> list[NormalizedTx]:
    """Swan's on-chain withdrawals export: Created At, Timezone, Transaction ID, Executed At,
    Canceled At, Status, Bitcoin Amount, ... Every settled row is a disposal (SELL by default);
    canceled rows (e.g. primetrust-canceled) never moved coins and are dropped. The Transaction
    ID is the on-chain txid — carried into the `txid` field so that when you load the receiving
    self-custody wallet, the reconciler connects the two and upgrades this to a transfer."""
    out = []
    for r in rows:
        status = _get(r, "status").lower()
        if status and status != "settled":            # only settled withdrawals actually executed
            continue
        txid = _get(r, "transaction id", "txid", "on-chain txid") or None
        out.append(NormalizedTx(
            kind=TxKind.SELL,
            # Executed/settled time is when coins left; fall back to creation time.
            timestamp=_dt(_get(r, "executed at", "created at", "date", "timestamp")),
            amount_sats=_to_sats(_get(r, "bitcoin amount", "btc amount", "amount", "btc")),
            txid=txid,
            address=_get(r, "destination", "destination address", "address") or None,
            counterparty="Swan",
            external_id=txid,                          # None -> persist falls back to a stable hash
            raw=dict(r),
        ))
    return out


def parse_bisq(rows: list[dict]) -> list[NormalizedTx]:
    out = []
    for r in rows:
        kind = _map_kind(_CUSTODIAL_KIND, _get(r, "type", "trade type", "direction"))
        if not kind:
            continue
        out.append(NormalizedTx(
            kind=kind,
            timestamp=_dt(_get(r, "date", "date/time", "timestamp")),
            amount_sats=_to_sats(_get(r, "amount in btc", "btc", "amount")),
            fiat_value=_usd(_get(r, "amount", "value", "amount in usd")),
            price_usd=_usd(_get(r, "price")),
            counterparty="Bisq",
            external_id=_get(r, "trade id", "id") or None,
            raw=dict(r),
        ))
    return out


PARSERS = {
    "generic": parse_generic,
    "coinbase": parse_coinbase,
    "strike": parse_strike,
    "swan": parse_swan,
    "bisq": parse_bisq,
}


def import_csv(
    session: Session, *, account_id: int, source: str, text: str, wallet_id: int | None = None
) -> ImportResult:
    result = ImportResult()
    parser = PARSERS.get(source)
    if parser is None:
        result.errors.append(f"unknown source: {source}")
        return result

    try:
        reader = csv.DictReader(io.StringIO(_strip_preamble(text)))
        rows = [_norm_keys(row) for row in reader]  # normalize keys/values once for every parser
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"could not parse CSV: {exc}")
        return result

    try:
        records = parser(rows)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"parser error: {exc}")
        return result

    # Rows the parser dropped entirely (unrecognized transaction type, or a non-BTC asset) —
    # surface them instead of silently omitting (they used to look like "skipped duplicates").
    ignored = len(rows) - len(records)
    if ignored > 0:
        result.rejected.append(f"{ignored} row(s) ignored (unrecognized transaction type or non-BTC asset).")

    persist_records(session, account_id, f"csv:{source}", records, result, wallet_id=wallet_id)
    return result


def persist_records(
    session: Session, account_id: int, source: str, records: list[NormalizedTx],
    result: ImportResult | None = None, wallet_id: int | None = None,
) -> ImportResult:
    """Persist normalized records under `source` (e.g. 'csv:coinbase').

    Idempotent: (account_id, source, external_id-or-stable-hash) uniqueness skips duplicates.
    """
    result = result or ImportResult()
    # Preload this source's existing external_ids once, so we de-dupe in Python and add new rows
    # in a SINGLE transaction (was a commit per row + an IntegrityError round-trip per duplicate).
    from sqlalchemy import select
    from app.models import Transaction
    seen = set(session.scalars(select(Transaction.external_id).where(
        Transaction.account_id == account_id, Transaction.source == source)))
    added = False
    for n in records:
        # Reject (with a reason) rather than silently coerce — a wrong date lands in the wrong
        # tax year; a zeroed amount corrupts the ledger.
        if n.timestamp is None:
            result.rejected.append(f"{n.kind}: skipped — unrecognized date.")
            continue
        if n.amount_sats == 0 and n.kind != TxKind.FEE:
            result.rejected.append(f"{n.kind} on {n.timestamp:%Y-%m-%d}: skipped — zero/invalid amount.")
            continue
        ext = n.external_id or _stable_id(source, n)
        if ext in seen:                 # duplicate (re-import) or repeated within this file
            result.skipped += 1
            continue
        seen.add(ext)
        tx_svc.add_transaction(
            session, account_id=account_id, wallet_id=wallet_id, kind=n.kind,
            timestamp=n.timestamp, amount_sats=n.amount_sats, fee_sats=n.fee_sats,
            price_usd=n.price_usd, fiat_value=n.fiat_value, fiat_fee=n.fiat_fee,
            fiat_source="actual",  # exchange-reported USD is the real transacted amount
            txid=n.txid, address=n.address, counterparty=n.counterparty,
            acquired_at=n.acquired_at,
            # A custodian-provided cost basis on a transfer-in is the carried basis for that lot.
            carried_basis_usd=(n.cost_basis_usd if n.kind == TxKind.TRANSFER_IN else None),
            raw_import=(json.dumps(n.raw) if n.raw else None),
            source=source, external_id=ext, note=n.note, commit=False,
        )
        result.imported += 1
        added = True
    if added:
        session.commit()
    return result
