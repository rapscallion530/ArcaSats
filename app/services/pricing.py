# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Historical BTC/USD prices.

A user-chosen price SOURCE (Settings → price source) determines where FMV comes from:
  - "coinbase" / "bitstamp": public exchange OHLC, fetched in fixed WEEKLY windows of
    15-minute candles (privacy: a request reveals only the week of activity, not exact tx
    times); the 15m candle CLOSE covering a transaction is its FMV.
  - "mempool": the user's OWN node's mempool instance (`/api/v1/historical-price`) — fully
    local, no third party.
Caches: PricePoint (daily close fallback) + HourlyPrice (reused as a 15-min candle cache).
Nothing is fetched unless BTT_ENABLE_NETWORK=1. The auto value is a SPOT estimate — a user's
actual price paid (premium/spread) should be entered and always takes precedence.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.models import SATS_PER_BTC, HourlyPrice, PricePoint, Transaction, TxKind

# Failures we expect from a public price endpoint (network down, rate-limited, schema drift).
# We swallow these into "no price available"; anything else propagates so real bugs surface.
_FETCH_ERRORS = (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                 ValueError, IndexError, KeyError, TypeError)  # OSError covers Tor socket failures

_VALUE_KINDS = {TxKind.BUY, TxKind.SELL, TxKind.INCOME, TxKind.SPEND}
# fiat_value provenance we must NOT overwrite during a price backfill.
_LOCKED_SOURCES = {"actual", "manual"}

# The FMV used for a transaction is the close of the 15-minute candle covering its time.
_GRANULARITY_SECONDS = 900  # 15-minute candles
# Per-source metadata (host, candle/daily fetchers, week-warm chunk size) lives on the SOURCES
# registry near the bottom of this module — the single source of truth, also driving the Settings
# dropdown and config validation. PRICE_SOURCES is derived from it there.


# --- daily-close cache (PricePoint) + CSV import -----------------------------
def get_cached(session: Session, d: dt.date) -> Decimal | None:
    return session.scalar(select(PricePoint.price_usd).where(PricePoint.date == d))


def upsert(session: Session, d: dt.date, price: Decimal, source: str = "manual") -> None:
    pp = session.scalar(select(PricePoint).where(PricePoint.date == d))
    if pp:
        pp.price_usd = price
        pp.source = source
    else:
        session.add(PricePoint(date=d, price_usd=price, source=source))
    session.commit()


def import_price_csv(session: Session, text: str) -> int:
    """Import date,price rows. Accepts headers like date/day and price/close/usd."""
    reader = csv.DictReader(io.StringIO(text))
    n = 0
    for row in reader:
        r = {(k or "").strip().lower(): v for k, v in row.items()}
        ds = r.get("date") or r.get("day") or r.get("time")
        ps = r.get("price") or r.get("close") or r.get("usd") or r.get("price_usd")
        if not ds or not ps:
            continue
        try:
            d = dt.date.fromisoformat(str(ds)[:10])
            price = Decimal(str(ps).replace(",", "").replace("$", "")).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            continue
        upsert(session, d, price, source="csv")
        n += 1
    return n


# --- price source + 15-min candle cache --------------------------------------
def _bucket_start(ts: dt.datetime) -> dt.datetime:
    """Floor a naive-UTC timestamp to its 15-minute candle bucket."""
    epoch = int(ts.replace(tzinfo=dt.UTC).timestamp())
    floored = epoch - (epoch % _GRANULARITY_SECONDS)
    return dt.datetime.fromtimestamp(floored, dt.UTC).replace(tzinfo=None)


def _price_source(session: Session) -> str:
    from app.services import node_settings
    src = (node_settings.get_config(session).price_source or "").strip().lower()
    return src if src in PRICE_SOURCES else "coinbase"


def get_cached_hour(session: Session, bucket_start: dt.datetime) -> Decimal | None:
    # HourlyPrice is reused as a 15-min candle cache (one row per bucket start).
    return session.scalar(select(HourlyPrice.price_usd).where(HourlyPrice.hour_start == bucket_start))


def upsert_hour(session: Session, bucket_start: dt.datetime, price: Decimal, source: str = "coinbase") -> None:
    hp = session.scalar(select(HourlyPrice).where(HourlyPrice.hour_start == bucket_start))
    if hp:
        hp.price_usd = price
        hp.source = source
    else:
        session.add(HourlyPrice(hour_start=bucket_start, price_usd=price, source=source))
    session.commit()


# --- shared HTTP helper ------------------------------------------------------
_HTTP_HEADERS = {"User-Agent": "bitcoin-tax-tracker", "Accept": "application/json"}


def _get_json(url: str, timeout: float = 12.0):
    """GET `url` and parse JSON — the HTTP boilerplate shared by every price fetcher. Callers
    wrap this in their own try/`_FETCH_ERRORS` and interpret the shape; `BTT_ENABLE_NETWORK` is
    gated by the callers before they reach here."""
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())


# --- daily close (per-date fallback when a 15m candle is missing) ------------
def _fetch_coinbase_daily(d: dt.date, timeout: float = 12.0) -> Decimal | None:
    """Coinbase Exchange daily close (keyless). BTT_ENABLE_NETWORK only. Public data, no PII."""
    if not config.ENABLE_NETWORK:
        return None
    url = (f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400"
           f"&start={d:%Y-%m-%d}T00:00:00Z&end={d:%Y-%m-%d}T23:59:59Z")
    try:
        rows = _get_json(url, timeout)
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], list) or len(rows[0]) < 5:
            return None
        return Decimal(str(rows[0][4])).quantize(Decimal("0.01"))  # candle = [time,low,high,open,close,vol]
    except _FETCH_ERRORS:
        return None


def _fetch_bitstamp_daily(d: dt.date, timeout: float = 12.0) -> Decimal | None:
    if not config.ENABLE_NETWORK:
        return None
    start = int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.UTC).timestamp())
    url = f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=1&start={start}"
    try:
        body = _get_json(url, timeout)
        ohlc = (body.get("data") or {}).get("ohlc") or []
        return Decimal(str(ohlc[0]["close"])).quantize(Decimal("0.01")) if ohlc else None
    except _FETCH_ERRORS:
        return None


def get_price(session: Session, d: dt.date, allow_network: bool = True, source: str = "coinbase") -> Decimal | None:
    """Daily-close fallback for a date, cached in PricePoint. Uses the chosen third-party
    source so a privacy-conscious choice isn't undone by querying a different exchange. A local
    source (mempool) has no daily fallback and returns None."""
    cached = get_cached(session, d)
    if cached is not None:
        return cached
    if not (allow_network and config.ENABLE_NETWORK):
        return None
    src = SOURCES.get(source)
    if src is None or src.is_local or src.daily_fetcher is None:
        return None
    from app.services import outbound
    outbound.record(src.host, "historical BTC/USD price fetch", "1 day (fallback)")
    p = src.daily_fetcher(d)
    if p is not None:
        upsert(session, d, p, source=source)
    return p


# --- 15m candle fetchers: (bucket_start, close) within [start, end) ----------
def _fetch_coinbase_candles(start: dt.datetime, end: dt.datetime, timeout: float = 12.0):
    if not config.ENABLE_NETWORK:
        return []
    url = (f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity={_GRANULARITY_SECONDS}"
           f"&start={start:%Y-%m-%dT%H:%M:%S}Z&end={end:%Y-%m-%dT%H:%M:%S}Z")
    try:
        rows = _get_json(url, timeout)
        if not isinstance(rows, list):
            return []
        out = []
        for r in rows:
            if isinstance(r, list) and len(r) >= 5:
                b = _bucket_start(dt.datetime.fromtimestamp(int(r[0]), dt.UTC).replace(tzinfo=None))
                out.append((b, Decimal(str(r[4])).quantize(Decimal("0.01"))))
        return out
    except _FETCH_ERRORS:
        return []


def _fetch_bitstamp_candles(start: dt.datetime, end: dt.datetime, timeout: float = 12.0):
    if not config.ENABLE_NETWORK:
        return []
    s = int(start.replace(tzinfo=dt.UTC).timestamp())
    e = int(end.replace(tzinfo=dt.UTC).timestamp())
    url = (f"https://www.bitstamp.net/api/v2/ohlc/btcusd/?step={_GRANULARITY_SECONDS}&limit=1000"
           f"&start={s}&end={e}")
    try:
        body = _get_json(url, timeout)
        ohlc = (body.get("data") or {}).get("ohlc") or []
        out = []
        for c in ohlc:
            b = _bucket_start(dt.datetime.fromtimestamp(int(c["timestamp"]), dt.UTC).replace(tzinfo=None))
            out.append((b, Decimal(str(c["close"])).quantize(Decimal("0.01"))))
        return out
    except _FETCH_ERRORS:
        return []


# --- price-source registry (single source of truth) --------------------------
@dataclass(frozen=True)
class PriceSource:
    """One FMV source. Third-party sources (coinbase/bitstamp) fetch 15m candles in weekly windows
    plus a daily-close fallback; a local source (mempool) is queried per bucket for the nearest
    stored price and has no daily fallback. All per-source knowledge lives here, so adding/removing
    a source is a single registry entry — and the same registry drives config validation
    (node_settings) and the Settings dropdown."""
    name: str
    label: str                                  # Settings dropdown label
    is_local: bool                              # local node: per-bucket nearest, no daily fallback
    host: str = ""                              # third-party API host (for the Outbound Log)
    max_hours: int = 0                          # week-warm request chunk (under the API candle cap)
    candle_fetcher: Callable | None = None      # (start, end) -> [(bucket_start, close)]
    daily_fetcher: Callable | None = None       # (date) -> Decimal | None


SOURCES: dict[str, PriceSource] = {s.name: s for s in (
    # Chunk sizes stay under each API's max-candles cap at 15m (Coinbase 300 -> 75h; Bitstamp
    # 1000 -> 250h); conservative values leave headroom.
    PriceSource("coinbase", "Coinbase (public, 15-min candles)", False,
                "api.exchange.coinbase.com", 72, _fetch_coinbase_candles, _fetch_coinbase_daily),
    PriceSource("bitstamp", "Bitstamp (public, 15-min candles)", False,
                "www.bitstamp.net", 240, _fetch_bitstamp_candles, _fetch_bitstamp_daily),
    PriceSource("mempool", "My own mempool node (local, no third party)", True),
)}
PRICE_SOURCES = tuple(SOURCES)


def price_source_choices() -> list[tuple[str, str]]:
    """(name, label) pairs for the Settings dropdown — derived from the registry."""
    return [(s.name, s.label) for s in SOURCES.values()]


def _week_start(ts: dt.datetime) -> dt.datetime:
    d = (ts - dt.timedelta(days=ts.weekday())).date()  # Monday of that week
    return dt.datetime(d.year, d.month, d.day)


def _warm_third_party(session: Session, src: PriceSource, buckets_needed: set[dt.datetime]) -> None:
    """For each fixed Mon–Sun WEEK containing a needed-but-uncached 15m bucket, download the whole
    week's 15m candles (the request reveals only the week of activity, not the exact tx time).
    Collected candles are written in a SINGLE bulk commit — one existence check over the range,
    insert only missing buckets — instead of a SELECT + commit per candle."""
    if not config.ENABLE_NETWORK:
        return
    weeks = sorted({_week_start(b) for b in buckets_needed if get_cached_hour(session, b) is None})
    if not weeks:
        return
    from app.services import outbound
    outbound.record(src.host, "historical BTC/USD price fetch", f"{len(weeks)} week(s)")
    step = dt.timedelta(hours=src.max_hours)
    collected: dict[dt.datetime, Decimal] = {}
    for wk in weeks:
        end = wk + dt.timedelta(days=7)
        cur = wk
        while cur < end:
            chunk_end = min(cur + step, end)
            for b, close in src.candle_fetcher(cur, chunk_end):
                collected.setdefault(b, close)
            cur = chunk_end
            time.sleep(0.35)  # be gentle between calls
    if not collected:
        return
    # One range query for existing buckets (avoids the SQLite 999-variable IN limit), then insert
    # only the missing ones in a single commit — same net cache as the old per-candle skip.
    existing = set(session.scalars(select(HourlyPrice.hour_start).where(
        HourlyPrice.hour_start >= min(collected), HourlyPrice.hour_start <= max(collected))))
    for b, close in collected.items():
        if b not in existing:
            session.add(HourlyPrice(hour_start=b, price_usd=close, source=src.name))
    session.commit()


# --- local mempool source ----------------------------------------------------
def _fetch_mempool_price(mempool_url: str, ts: dt.datetime, timeout: float = 12.0,
                         *, proxy_host: str | None = None, proxy_port: int | None = None) -> Decimal | None:
    """Nearest stored BTC/USD price from the user's own mempool instance. Local, no third party.
    Routes over the Tor SOCKS proxy when proxy_host is set (for a .onion mempool); clearnet keeps
    the urllib path so it stays simple/monkeypatchable."""
    if not mempool_url:
        return None
    unix = int(ts.replace(tzinfo=dt.UTC).timestamp())
    url = f"{mempool_url.rstrip('/')}/api/v1/historical-price?currency=USD&timestamp={unix}"
    try:
        if proxy_host:
            from app.services import http_fetch
            body = http_fetch.get_json(url, proxy_host=proxy_host, proxy_port=proxy_port, timeout=timeout)
        else:
            body = _get_json(url, timeout)
        prices = body.get("prices") or []
        if not prices:
            return None
        usd = prices[0].get("USD")
        return Decimal(str(usd)).quantize(Decimal("0.01")) if usd else None
    except _FETCH_ERRORS:
        return None


def _warm_mempool(session: Session, mempool_url: str, buckets_needed: set[dt.datetime],
                  *, proxy_host: str | None = None, proxy_port: int | None = None) -> None:
    """Query the user's mempool for each needed-but-uncached bucket. Per-bucket QUERIES are fine
    here: it's the user's OWN node, so there's no third-party fingerprinting concern. The DB
    writes are batched into a single commit at the end. Routes over Tor when proxy_host is set."""
    if not config.ENABLE_NETWORK or not mempool_url:
        return
    todo = sorted(b for b in buckets_needed if get_cached_hour(session, b) is None)
    if not todo:
        return
    from urllib.parse import urlparse
    from app.services import outbound
    outbound.record(urlparse(mempool_url).hostname or mempool_url,
                    "BTC/USD price (own mempool%s)" % (" over Tor" if proxy_host else ""),
                    f"{len(todo)} lookup(s)")
    added = False
    for b in todo:  # `todo` is already the uncached set, so a plain insert won't duplicate
        p = _fetch_mempool_price(mempool_url, b, proxy_host=proxy_host, proxy_port=proxy_port)
        if p is not None:
            session.add(HourlyPrice(hour_start=b, price_usd=p, source="mempool"))
            added = True
        time.sleep(0.05)
    if added:
        session.commit()


def _warm(session: Session, src: PriceSource, buckets_needed: set[dt.datetime], cfg) -> None:
    """Warm the 15m candle cache for the chosen source (single dispatch point — callers don't
    branch on source name)."""
    if src.is_local:
        from urllib.parse import urlparse
        from app.services import http_fetch
        host = urlparse(cfg.mempool_url or "").hostname or ""
        via = http_fetch.via_tor(host, getattr(cfg, "mempool_use_tor", False))
        _warm_mempool(session, cfg.mempool_url, buckets_needed,
                      proxy_host=cfg.tor_host if via else None,
                      proxy_port=cfg.tor_port if via else None)
    else:
        _warm_third_party(session, src, buckets_needed)


def price_at(session: Session, ts: dt.datetime, source: str = "coinbase",
             allow_network: bool = True) -> tuple[Decimal | None, str | None]:
    """FMV for a transaction's time: the close of its 15m candle from the cache, else the
    daily-close fallback (third-party sources only — mempool stays fully local)."""
    cached = get_cached_hour(session, _bucket_start(ts))
    if cached is not None:
        return cached, "candle"
    src = SOURCES.get(source)
    if src is None or src.is_local:
        return None, None  # local: warm already asked the user's node for the nearest; no fallback
    daily = get_price(session, ts.date(), allow_network=allow_network, source=source)
    if daily is not None:
        return daily, "daily"
    return None, None


@dataclass
class PriceBackfillResult:
    updated: int = 0
    missing: int = 0
    network_used: bool = False
    used_daily_fallback: bool = False
    note: str = ""


def _locked(tx: Transaction) -> bool:
    """True if the tx's USD value is authoritative (exchange CSV or user-entered) and must
    never be overwritten by the price backfill."""
    return tx.fiat_source in _LOCKED_SOURCES


def backfill_prices(session: Session, account_id: int) -> PriceBackfillResult:
    """Set each transaction's USD price from the chosen price source (15m candle close, daily
    close as fallback). Only estimates what isn't already authoritative: `actual`/`manual`
    rows keep their value (they only gain a reference price_usd if missing). Online fetch
    requires BTT_ENABLE_NETWORK=1; mempool source also requires a configured mempool URL.

    The auto value is a SPOT estimate; a user who paid a premium/spread should enter their
    actual price (it becomes `manual` and is never overwritten)."""
    from app.services import node_settings
    res = PriceBackfillResult(network_used=config.ENABLE_NETWORK)
    source = _price_source(session)
    cfg = node_settings.get_config(session)
    mempool_url = cfg.mempool_url
    txs = session.scalars(select(Transaction).where(Transaction.account_id == account_id)).all()

    def needs(tx):
        if _locked(tx):
            return tx.price_usd is None  # only a missing reference price
        needs_value = tx.kind in _VALUE_KINDS and tx.amount_sats and (
            tx.fiat_value is None or tx.fiat_source == "estimate")
        return tx.price_usd is None or needs_value

    work = [tx for tx in txs if needs(tx)]

    # Warm the candle cache from the chosen source (third-party: whole WEEKS; mempool: local).
    if config.ENABLE_NETWORK and work:
        needed = {_bucket_start(tx.timestamp) for tx in work if not _locked(tx)}
        src = SOURCES.get(source)
        if src is not None:
            _warm(session, src, needed, cfg)

    for tx in work:
        before = (tx.price_usd, tx.fiat_value, tx.fiat_source)
        if _locked(tx):
            if tx.price_usd is None and tx.fiat_value is not None and tx.amount_sats:
                tx.price_usd = (tx.fiat_value * SATS_PER_BTC / Decimal(tx.amount_sats)).quantize(Decimal("0.01"))
        else:
            price, gran = price_at(session, tx.timestamp, source=source, allow_network=config.ENABLE_NETWORK)
            if price is None:
                res.missing += 1
                continue
            if gran == "daily":
                res.used_daily_fallback = True
            tx.price_usd = price
            if tx.kind in _VALUE_KINDS and tx.amount_sats:
                tx.fiat_value = (price * Decimal(tx.amount_sats) / SATS_PER_BTC).quantize(Decimal("0.01"))
                tx.fiat_source = "estimate"
        if (tx.price_usd, tx.fiat_value, tx.fiat_source) != before:
            res.updated += 1

    session.commit()
    if source == "mempool" and not mempool_url and res.missing:
        res.note = "Price source is 'mempool' but no mempool URL is set in Settings → Node."
    elif not config.ENABLE_NETWORK and res.missing:
        res.note = "Online price fetch is off. Set BTT_ENABLE_NETWORK=1, or import a price CSV."
    elif res.used_daily_fallback:
        res.note = "Some prices used the daily close (a 15-minute candle wasn't available)."
    return res
