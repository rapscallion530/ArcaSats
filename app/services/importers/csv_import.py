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
    s = s.replace("$", "").replace(",", "").replace("USD", "").strip()
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
    bare = s.replace("Z", "")
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
_GENERIC_KIND = {
    "buy": TxKind.BUY, "purchase": TxKind.BUY, "sell": TxKind.SELL, "sale": TxKind.SELL,
    "income": TxKind.INCOME, "reward": TxKind.INCOME, "rewards": TxKind.INCOME, "interest": TxKind.INCOME,
    "spend": TxKind.SPEND, "payment": TxKind.SPEND,
    "transfer_in": TxKind.TRANSFER_IN, "deposit": TxKind.TRANSFER_IN, "receive": TxKind.TRANSFER_IN,
    "transfer_out": TxKind.TRANSFER_OUT, "withdrawal": TxKind.TRANSFER_OUT, "send": TxKind.TRANSFER_OUT,
    "withdraw": TxKind.TRANSFER_OUT, "fee": TxKind.FEE,
}

_COINBASE_KIND = {
    "buy": TxKind.BUY, "advanced trade buy": TxKind.BUY, "advance trade buy": TxKind.BUY,
    "sell": TxKind.SELL, "advanced trade sell": TxKind.SELL,
    "receive": TxKind.TRANSFER_IN, "send": TxKind.TRANSFER_OUT,
    "rewards income": TxKind.INCOME, "reward income": TxKind.INCOME, "staking income": TxKind.INCOME,
    "learning reward": TxKind.INCOME, "coinbase earn": TxKind.INCOME, "inflation reward": TxKind.INCOME,
    "convert": TxKind.SELL,
}


def _map_kind(table: dict, raw: str) -> str | None:
    return table.get((raw or "").strip().lower())


# --- parsers -----------------------------------------------------------------
def parse_generic(rows: list[dict]) -> list[NormalizedTx]:
    out = []
    for row in rows:
        r = _norm_keys(row)
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
            counterparty=_get(r, "counterparty", "source", "exchange"),
            external_id=_get(r, "external_id", "id", "reference") or None,
            note=_get(r, "note", "notes", "memo"),
        ))
    return out


def parse_coinbase(rows: list[dict]) -> list[NormalizedTx]:
    out = []
    for row in rows:
        r = _norm_keys(row)
        asset = _get(r, "asset", "currency").upper()
        if asset and asset != "BTC":
            continue
        kind = _map_kind(_COINBASE_KIND, _get(r, "transaction type", "type"))
        if not kind:
            continue
        out.append(NormalizedTx(
            kind=kind,
            timestamp=_dt(_get(r, "timestamp", "date")),
            amount_sats=_to_sats(_get(r, "quantity transacted", "quantity", "amount")),
            fiat_value=_usd(_get(r, "total (inclusive of fees and/or spread)", "total", "subtotal")),
            fiat_fee=_usd(_get(r, "fees and/or spread", "fees", "fee")),
            price_usd=_usd(_get(r, "spot price at transaction", "spot price", "price")),
            counterparty="Coinbase",
            note=_get(r, "notes", "note"),
        ))
    return out


def parse_strike(rows: list[dict]) -> list[NormalizedTx]:
    """Strike Annual Account Statement: Transaction ID, Time (UTC), Status, Transaction Type,
    Amount USD, Fee USD, Amount BTC, Fee BTC, Description, Exchange Rate, Transaction Hash.

    The statement interleaves BTC rows (Purchase, on-chain Send) with USD-only rows: fiat
    Deposit/Withdrawal (funding the USD balance) and USD-denominated Lightning Sends (whose BTC
    size isn't in the export). USD-only rows are skipped — recording a 0-BTC ledger entry would
    corrupt balances/basis. Pending/failed rows are skipped too. (The older idealized
    `Time (UTC),Transaction Type,Amount BTC,Amount USD,BTC Price,Fee,Reference` header with a BTC
    amount on every row still imports unchanged.)"""
    out = []
    for row in rows:
        r = _norm_keys(row)
        status = _get(r, "status").lower()
        if status and status not in ("completed", "complete", "settled"):
            continue
        kind = _map_kind(_GENERIC_KIND, _get(r, "transaction type", "type", "event"))
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
            txid=_get(r, "transaction hash", "txid", "on-chain txid", "destination") or None,
            counterparty="Strike",
            external_id=_get(r, "transaction id", "reference", "id") or None,
        ))
    return out


def parse_swan(rows: list[dict]) -> list[NormalizedTx]:
    """Swan ships two unrelated exports. The transactions/transfers export has an `Event`
    column; the on-chain withdrawals export has none (identify it by its own columns)."""
    if not rows:
        return []
    cols = {(k or "").strip().lower() for k in rows[0].keys()}
    if "bitcoin amount" in cols and "created at" in cols:
        return _parse_swan_withdrawals(rows)
    return _parse_swan_transactions(rows)


def _parse_swan_transactions(rows: list[dict]) -> list[NormalizedTx]:
    """Swan's transactions export: Event, Date, ..., Unit Count, Asset Type, BTC Price, ...
    It interleaves BTC rows with USD rows (fiat funding deposits, monthly fees) — only the
    BTC-asset rows are ledger events, so non-BTC rows are filtered out. (The older idealized
    `BTC Amount`/`USD Amount` header with no Asset Type column still works: an absent Asset
    Type is treated as BTC, and the amount/value fall through to the legacy column names.)"""
    out = []
    for row in rows:
        r = _norm_keys(row)
        asset = _get(r, "asset type", "asset")
        if asset and asset.upper() != "BTC":          # USD funding / fees -> not a BTC event
            continue
        kind = _map_kind(_GENERIC_KIND, _get(r, "event", "type", "transaction type"))
        if not kind:
            continue
        out.append(NormalizedTx(
            kind=kind,
            timestamp=_dt(_get(r, "date", "timestamp", "time")),
            amount_sats=_to_sats(_get(r, "unit count", "btc amount", "amount btc", "amount", "btc")),
            fiat_value=_usd(_get(r, "transaction usd", "total usd", "usd amount", "usd", "value")),
            fiat_fee=_usd(_get(r, "fee usd", "fee")),
            price_usd=_usd(_get(r, "btc price", "price")),
            counterparty="Swan",
            external_id=_get(r, "transaction id", "id", "reference") or None,
        ))
    return out


def _parse_swan_withdrawals(rows: list[dict]) -> list[NormalizedTx]:
    """Swan's on-chain withdrawals export: Created At, Timezone, Transaction ID, Executed At,
    Canceled At, Status, Bitcoin Amount, ... Every settled row is a transfer_out; canceled
    rows (e.g. primetrust-canceled) never moved coins and are dropped. The Transaction ID is
    the on-chain txid — carried into the `txid` field so a synced self-custody wallet's
    matching transfer_in reconciles as an internal transfer (see costbasis.internal_txids)."""
    out = []
    for row in rows:
        r = _norm_keys(row)
        status = _get(r, "status").lower()
        if status and status != "settled":            # only settled withdrawals actually executed
            continue
        txid = _get(r, "transaction id", "txid", "on-chain txid") or None
        out.append(NormalizedTx(
            kind=TxKind.TRANSFER_OUT,
            # Executed/settled time is when coins left; fall back to creation time.
            timestamp=_dt(_get(r, "executed at", "created at", "date", "timestamp")),
            amount_sats=_to_sats(_get(r, "bitcoin amount", "btc amount", "amount", "btc")),
            txid=txid,
            counterparty="Swan",
            external_id=txid,                          # None -> persist falls back to a stable hash
        ))
    return out


def parse_bisq(rows: list[dict]) -> list[NormalizedTx]:
    out = []
    for row in rows:
        r = _norm_keys(row)
        kind = _map_kind(_GENERIC_KIND, _get(r, "type", "trade type", "direction"))
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
        rows = list(reader)
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
        tx = tx_svc.add_transaction(
            session, account_id=account_id, wallet_id=wallet_id, kind=n.kind,
            timestamp=n.timestamp, amount_sats=n.amount_sats, fee_sats=n.fee_sats,
            price_usd=n.price_usd, fiat_value=n.fiat_value, fiat_fee=n.fiat_fee,
            fiat_source="actual",  # exchange-reported USD is the real transacted amount
            txid=n.txid, address=n.address, counterparty=n.counterparty,
            source=source, external_id=ext, note=n.note,
        )
        if tx is None:
            result.skipped += 1
        else:
            result.imported += 1
    return result
