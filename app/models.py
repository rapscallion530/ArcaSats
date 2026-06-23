# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Domain models.

Money conventions:
  - Bitcoin amounts are stored as INTEGER satoshis (no floats).
  - Fiat (USD) amounts are stored as Numeric/Decimal.
  - Timestamps are UTC (stored naive, interpreted as UTC).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

SATS_PER_BTC = 100_000_000


class FiatSource:
    """Provenance of a transaction's USD value (see Transaction.fiat_source). Kept as a
    constants class (like TxKind) so importers/services don't hardcode magic strings."""
    ACTUAL = "actual"      # from an exchange CSV/API — the real transacted USD
    MANUAL = "manual"      # the user typed it in
    ESTIMATE = "estimate"  # derived from the BTC/USD price feed (may be upgraded)
    # Values that the price backfill must never overwrite:
    LOCKED = (ACTUAL, MANUAL)


# --- Transaction kinds -------------------------------------------------------
class TxKind:
    BUY = "buy"                 # acquisition; basis = fiat_value + fiat_fee
    SELL = "sell"               # taxable disposal; proceeds = fiat_value - fiat_fee
    INCOME = "income"           # acquisition + ordinary income at FMV
    SPEND = "spend"             # taxable disposal at FMV (goods/services, gift over limit)
    TRANSFER_IN = "transfer_in"   # non-taxable move into a wallet (basis carried/zeroed)
    TRANSFER_OUT = "transfer_out" # non-taxable move out of a wallet
    OPENING = "opening"         # opening balance / pre-2025 (Rev. Proc. 2024-28) starting lot
    FEE = "fee"                 # standalone network fee (rare; usually folded into a tx)

    ALL = (BUY, SELL, INCOME, SPEND, TRANSFER_IN, TRANSFER_OUT, OPENING, FEE)
    ACQUISITIONS = (BUY, INCOME, TRANSFER_IN, OPENING)
    DISPOSALS = (SELL, SPEND)          # the taxable ones (Form 8949)
    LABELS = {
        BUY: "Buy", SELL: "Sell", INCOME: "Income", SPEND: "Spend",
        TRANSFER_IN: "Transfer in", TRANSFER_OUT: "Transfer out",
        OPENING: "Opening balance", FEE: "Fee",
    }


class WalletType:
    XPUB = "xpub"
    CSV = "csv"
    API = "api"
    MANUAL = "manual"
    ALL = (XPUB, CSV, API, MANUAL)


# --- Tables ------------------------------------------------------------------
class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    label_kind: Mapped[str] = mapped_column(String(40), default="")  # e.g. "KYC", "non-KYC"
    # Whose coins these are. Blank = you (the primary holder). Different owners (e.g. a
    # family member's xpub) do NOT share cost basis — a transfer to them is a gift/disposal,
    # and they establish a fresh basis.
    owner: Mapped[str] = mapped_column(String(120), default="")
    lot_method: Mapped[str] = mapped_column(String(10), default="fifo")  # fifo / lifo / hifo
    note: Mapped[str] = mapped_column(Text, default="")
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))

    wallets: Mapped[list[Wallet]] = relationship(back_populates="account", cascade="all, delete-orphan")
    transactions: Mapped[list[Transaction]] = relationship(back_populates="account", cascade="all, delete-orphan")


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    label: Mapped[str] = mapped_column(String(120))
    wtype: Mapped[str] = mapped_column(String(20), default=WalletType.MANUAL)
    xpub: Mapped[str | None] = mapped_column(Text, nullable=True)
    script_type: Mapped[str] = mapped_column(String(20), default="")   # p2pkh/p2sh-p2wpkh/p2wpkh
    gap_limit: Mapped[int] = mapped_column(default=20)
    # How on-chain activity with an EXTERNAL counterparty is classified (see
    # docs/onchain-classification.md):
    #   "standalone"    -> external inflow = BUY, external outflow = SELL (taxable; this wallet's
    #                      coins are acquired/disposed externally and have no exchange CSV).
    #   "custodial_fed" -> external inflow/outflow = transfer (basis comes from an exchange CSV
    #                      you import, so don't double-count). True transfers between your own
    #                      loaded wallets are auto-detected in either mode.
    onchain_mode: Mapped[str] = mapped_column(String(20), default="standalone")
    # User override for the address encoding of a single-sig xpub: "auto" (detect from history)
    # or a forced "p2wpkh"/"p2sh-p2wpkh"/"p2pkh" (useful for a brand-new/empty wallet where
    # there's no history to detect from). Ignored for multisig descriptors (the descriptor
    # dictates the script). The `xpub` field above holds either an xpub or a full output
    # descriptor (wsh/sh/sh(wsh) of (sorted)multi) for multisig.
    address_type: Mapped[str] = mapped_column(String(20), default="auto")
    source_meta: Mapped[str] = mapped_column(Text, default="")          # JSON blob, importer-specific
    created_at: Mapped[dt.datetime] = mapped_column(default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))

    account: Mapped[Account] = relationship(back_populates="wallets")
    transactions: Mapped[list[Transaction]] = relationship(back_populates="wallet", cascade="all, delete-orphan")
    utxos: Mapped[list[Utxo]] = relationship(cascade="all, delete-orphan")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        # Dedupe imports PER ACCOUNT: the same exchange export imported into two different
        # accounts must not have the second silently dropped as a "duplicate".
        UniqueConstraint("account_id", "source", "external_id", name="uq_tx_account_source_external"),
        # The hot path: list/aggregate a single account's ledger in timestamp order.
        Index("ix_tx_account_ts", "account_id", "timestamp"),
        # internal_txids() / find_transfer_matches() join transfer rows by txid.
        Index("ix_tx_txid", "txid"),
        # Many queries filter by kind (acquisitions vs disposals vs transfers).
        Index("ix_tx_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id"), nullable=True, index=True)

    timestamp: Mapped[dt.datetime] = mapped_column()
    kind: Mapped[str] = mapped_column(String(20))

    amount_sats: Mapped[int] = mapped_column(default=0)          # magnitude of BTC moved
    fee_sats: Mapped[int] = mapped_column(default=0)

    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)   # per-BTC price
    fiat_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)  # total USD of amount
    fiat_fee: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # Provenance of fiat_value, so the price backfill knows what it may touch:
    #   "actual"   -> from an exchange CSV/API (the real transacted USD) — never overwritten
    #   "manual"   -> the user typed it in — never overwritten
    #   "estimate" -> derived from the BTC/USD price feed — may be upgraded (e.g. daily -> hourly)
    #   None       -> no fiat_value yet (estimate-eligible)
    fiat_source: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Cost basis carried in from a cross-account self-transfer (set by the reconciler).
    carried_basis_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    # If True, the user has opted this transfer_in OUT of basis carryover (use fresh basis).
    carry_disabled: Mapped[bool] = mapped_column(default=False)

    txid: Mapped[str | None] = mapped_column(String(80), nullable=True)
    address: Mapped[str | None] = mapped_column(String(120), nullable=True)
    counterparty: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(60), default="manual")    # manual / csv:coinbase / xpub / api:strike
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))

    account: Mapped[Account] = relationship(back_populates="transactions")
    wallet: Mapped[Wallet | None] = relationship(back_populates="transactions")

    @property
    def amount_btc(self) -> Decimal:
        return Decimal(self.amount_sats) / SATS_PER_BTC

    @property
    def usd_value(self) -> Decimal | None:
        """The USD value to DISPLAY for this transaction, regardless of kind: the explicit
        fiat_value if recorded, otherwise the fair-market value at the time (reference
        price x amount). The transaction's KIND decides how this value is used in tax calc —
        cost basis (buy/opening/income), proceeds (sell/spend), or informational (transfers,
        where basis carries with the coins). Returns None only when no price is known yet."""
        if self.fiat_value is not None:
            return self.fiat_value
        if self.price_usd is not None:
            return (self.price_usd * self.amount_btc).quantize(Decimal("0.01"))
        return None

    @property
    def signed_sats(self) -> int:
        """Positive for inflows, negative for outflows (for balance math)."""
        if self.kind in TxKind.ACQUISITIONS:
            return self.amount_sats
        return -self.amount_sats


class Utxo(Base):
    """A single on-chain output owned by an xpub/descriptor wallet — the UTXO-level inventory
    that complements (does not replace) the net-per-tx Transaction ledger.

    Only on-chain wallets have UTXOs; custodial CSV sources are omnibus wallets with no
    output-level visibility. Provenance/privacy features (coin labels, KYC/non-KYC merge
    detection, address reuse, change tracking) read from this table; the cost-basis engine
    is unaffected. spent_txid IS NULL means the coin is currently unspent (a live UTXO).
    """
    __tablename__ = "utxos"
    __table_args__ = (
        # An outpoint is unique within a wallet (the same descriptor loaded as two wallets may
        # legitimately see the same outpoint, so we scope to the wallet rather than globally).
        UniqueConstraint("wallet_id", "txid", "vout", name="uq_utxo_wallet_outpoint"),
        Index("ix_utxo_account", "account_id"),
        Index("ix_utxo_spent_txid", "spent_txid"),   # group inputs of a spend (merge detection)
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)

    txid: Mapped[str] = mapped_column(String(80))      # creating transaction
    vout: Mapped[int] = mapped_column()                # output index within that tx
    value_sats: Mapped[int] = mapped_column(default=0)
    address: Mapped[str] = mapped_column(String(120), default="")
    script_type: Mapped[str] = mapped_column(String(24), default="")
    chain: Mapped[int] = mapped_column(default=0)      # 0 = receive, 1 = change
    deriv_index: Mapped[int] = mapped_column(default=0)
    is_change: Mapped[bool] = mapped_column(default=False)
    # Provenance snapshot of the owning account's label (e.g. "KYC" / "non-KYC"), refreshed
    # each sync so a relabel propagates. Drives the KYC/non-KYC coin-merge warning.
    label_kind: Mapped[str] = mapped_column(String(40), default="")

    created_height: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))
    spent_txid: Mapped[str | None] = mapped_column(String(80), nullable=True)
    spent_height: Mapped[int | None] = mapped_column(nullable=True)
    spent_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    @property
    def value_btc(self) -> Decimal:
        return Decimal(self.value_sats) / SATS_PER_BTC

    @property
    def spent(self) -> bool:
        return self.spent_txid is not None


class PricePoint(Base):
    """Local cache of historical BTC/USD daily prices (daily close, fallback)."""
    __tablename__ = "price_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[dt.date] = mapped_column(unique=True)
    price_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    source: Mapped[str] = mapped_column(String(40), default="")


class HourlyPrice(Base):
    """Local cache of historical BTC/USD hourly prices, keyed by UTC hour-bucket.

    Preferred over the daily close for estimating a transaction's value, since BTC
    can swing several percent within a day. hour_start is the tx timestamp truncated
    to the hour (naive UTC); price_usd is the close of that hourly candle.
    """
    __tablename__ = "hourly_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    hour_start: Mapped[dt.datetime] = mapped_column(unique=True)
    price_usd: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    source: Mapped[str] = mapped_column(String(40), default="")


class NodeConfig(Base):
    """Singleton (id=1) Electrum node connection, editable in Settings.

    Seeded from env on first use; the DB row then overrides env.
    """
    __tablename__ = "node_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    electrum_host: Mapped[str] = mapped_column(String(255), default="")
    electrum_port: Mapped[int] = mapped_column(default=50001)
    use_ssl: Mapped[bool] = mapped_column(default=False)
    use_tor: Mapped[bool] = mapped_column(default=False)
    tor_host: Mapped[str] = mapped_column(String(64), default="127.0.0.1")
    tor_port: Mapped[int] = mapped_column(default=9050)
    # Base URL of a block explorer (mempool.space-compatible) for "view on explorer" links —
    # your own node's mempool on StartOS/Umbrel, a LAN instance, or a .onion. Empty = no links.
    # The browser navigates there directly; ArcaSats sends nothing (only your browser + the txid).
    mempool_url: Mapped[str] = mapped_column(String(255), default="")
    # Where historical USD prices come from: "coinbase" / "bitstamp" (public exchange OHLC,
    # weekly 15m candles) or "mempool" (your own node's historical-price API — fully local).
    price_source: Mapped[str] = mapped_column(String(20), default="coinbase")
    updated_at: Mapped[dt.datetime] = mapped_column(default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))


class OutboundLog(Base):
    """Privacy ledger: every intentional outbound network action is recorded locally,
    so the user can see exactly what left the machine (never coin/PII — just host+purpose)."""
    __tablename__ = "outbound_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))
    host: Mapped[str] = mapped_column(String(255))
    purpose: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str] = mapped_column(String(255), default="")


class LLMConnection(Base):
    """A user-defined connection to a LOCAL large language model (Ollama or any
    OpenAI-compatible endpoint, e.g. LM Studio / llama.cpp). Used by the optional
    read-only "Ask your data" assistant. Off until the user adds one. `allow_remote`
    must be explicitly enabled to send data to a non-loopback/LAN endpoint."""
    __tablename__ = "llm_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    provider: Mapped[str] = mapped_column(String(20), default="ollama")  # ollama / openai
    base_url: Mapped[str] = mapped_column(String(255), default="http://127.0.0.1:11434")
    model: Mapped[str] = mapped_column(String(120), default="")
    api_key: Mapped[str] = mapped_column(String(255), default="")        # usually blank for local
    allow_remote: Mapped[bool] = mapped_column(default=False)
    is_default: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[dt.datetime] = mapped_column(default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))


class User(Base):
    """Multi-user (Phase 7). Present from the start so FKs resolve."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="member")  # admin / member
    # Bumped to invalidate all of a user's existing sessions (e.g. on password change /
    # "sign out everywhere"). Signed into the session token; a mismatch rejects the cookie.
    token_version: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[dt.datetime] = mapped_column(default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))
