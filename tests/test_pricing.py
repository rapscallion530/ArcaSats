"""Phase 4: price cache + CSV import (no network)."""
import datetime as dt
from decimal import Decimal

from app.services import pricing


def test_upsert_and_get_cached(session):
    d = dt.date(2025, 1, 1)
    assert pricing.get_cached(session, d) is None
    pricing.upsert(session, d, Decimal("90000.00"))
    assert pricing.get_cached(session, d) == Decimal("90000.00")
    # upsert overwrites
    pricing.upsert(session, d, Decimal("91000.00"), source="csv")
    assert pricing.get_cached(session, d) == Decimal("91000.00")


def test_import_price_csv(session):
    text = "date,price\n2025-01-01,90000\n2025-01-02,92500.50\nbad,row\n"
    n = pricing.import_price_csv(session, text)
    assert n == 2
    assert pricing.get_cached(session, dt.date(2025, 1, 2)) == Decimal("92500.50")


def test_backfill_prices_from_cache(session):
    import datetime as dt
    from app.models import TxKind
    from app.services import accounts as acc
    from app.services import transactions as txs
    a = acc.create_account(session, name="P")
    pricing.upsert(session, dt.date(2025, 1, 1), Decimal("90000"))
    buy = txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                              timestamp=dt.datetime(2025, 1, 1), amount_sats=txs.btc_to_sats("0.01"))
    tin = txs.add_transaction(session, account_id=a.id, kind=TxKind.TRANSFER_IN,
                              timestamp=dt.datetime(2025, 1, 1), amount_sats=txs.btc_to_sats("0.02"))
    res = pricing.backfill_prices(session, a.id)
    session.refresh(buy)
    session.refresh(tin)
    assert res.updated == 2
    assert buy.price_usd == Decimal("90000.00")
    assert buy.fiat_value == Decimal("900.00")        # value event -> fiat derived
    assert tin.price_usd == Decimal("90000.00")
    assert tin.fiat_value is None                      # transfer -> reference price only, no basis


class _FakeResp:
    """Minimal context-manager HTTP response for monkeypatching urlopen."""
    def __init__(self, payload):
        import json
        self._p = json.dumps(payload).encode()
    def read(self):
        return self._p
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_price_source_setting_roundtrip(session):
    from app.services import node_settings as ns
    kw = dict(electrum_host="", electrum_port=50001, use_ssl=False, use_tor=False,
              tor_host="127.0.0.1", tor_port=9050)
    assert ns.save_config(session, price_source="bitstamp", **kw).price_source == "bitstamp"
    # An invalid value is ignored (keeps the prior valid one).
    assert ns.save_config(session, price_source="garbage", **kw).price_source == "bitstamp"
    assert pricing._price_source(session) == "bitstamp"


def test_bitstamp_15m_candle_parse(monkeypatch):
    monkeypatch.setattr(pricing.config, "ENABLE_NETWORK", True)
    payload = {"data": {"ohlc": [{"timestamp": "1672531200", "close": "16500.00"}]}}  # 2023-01-01 00:00Z
    monkeypatch.setattr(pricing.urllib.request, "urlopen", lambda *a, **k: _FakeResp(payload))
    out = pricing._fetch_bitstamp_candles(dt.datetime(2023, 1, 1), dt.datetime(2023, 1, 1, 1))
    assert out == [(dt.datetime(2023, 1, 1, 0, 0), Decimal("16500.00"))]


def test_mempool_price_parse(monkeypatch):
    monkeypatch.setattr(pricing.config, "ENABLE_NETWORK", True)
    payload = {"prices": [{"time": 1672531200, "USD": 16500}], "exchangeRates": {}}
    monkeypatch.setattr(pricing.urllib.request, "urlopen", lambda *a, **k: _FakeResp(payload))
    v = pricing._fetch_mempool_price("http://node.local:3006", dt.datetime(2023, 1, 1))
    assert v == Decimal("16500.00")


def test_price_source_registry():
    # The registry is the single source of truth for the valid sources + their metadata.
    assert pricing.PRICE_SOURCES == tuple(pricing.SOURCES)
    assert set(pricing.PRICE_SOURCES) == {"coinbase", "bitstamp", "mempool"}
    assert all(s.label for s in pricing.SOURCES.values())          # every source has a UI label
    mp = pricing.SOURCES["mempool"]
    assert mp.is_local and mp.daily_fetcher is None and mp.candle_fetcher is None
    for name in ("coinbase", "bitstamp"):
        s = pricing.SOURCES[name]
        assert not s.is_local and s.host and s.candle_fetcher and s.daily_fetcher
    assert pricing.price_source_choices() == [(s.name, s.label) for s in pricing.SOURCES.values()]


def test_node_settings_validates_price_source_from_registry(session):
    from app.services import node_settings as ns
    kw = dict(electrum_host="", electrum_port=50001, use_ssl=False, use_tor=False,
              tor_host="127.0.0.1", tor_port=9050)
    for src in pricing.PRICE_SOURCES:                              # each registry source accepted
        assert ns.save_config(session, price_source=src, **kw).price_source == src
    # An unknown source is rejected (keeps the prior valid one) — validation derives from the registry.
    assert ns.save_config(session, price_source="kraken", **kw).price_source == pricing.PRICE_SOURCES[-1]


def test_warm_third_party_batches_into_cache(session, monkeypatch):
    # Guards the batched (single-commit) warm rewrite: a week-warm populates the 15m cache and is
    # idempotent (a second warm inserts nothing).
    monkeypatch.setattr(pricing.config, "ENABLE_NETWORK", True)
    monkeypatch.setattr(pricing.time, "sleep", lambda *a, **k: None)
    bucket = dt.datetime(2023, 1, 1, 0, 0)
    payload = [[1672531200, 1, 2, 3, 16500, 9]]   # [time, low, high, open, close, vol]
    monkeypatch.setattr(pricing.urllib.request, "urlopen", lambda *a, **k: _FakeResp(payload))
    pricing._warm_third_party(session, pricing.SOURCES["coinbase"], {bucket})
    assert pricing.get_cached_hour(session, bucket) == Decimal("16500.00")
    pricing._warm_third_party(session, pricing.SOURCES["coinbase"], {bucket})  # idempotent
    from sqlalchemy import func, select as _select
    from app.models import HourlyPrice
    n = session.scalar(_select(func.count()).select_from(HourlyPrice).where(HourlyPrice.hour_start == bucket))
    assert n == 1


def test_get_price_no_network_returns_none(session):
    # network disabled in tests -> no fallback, returns None for uncached date
    assert pricing.get_price(session, dt.date(2030, 5, 5), allow_network=True) is None


def test_hourly_cache_preferred_over_daily(session):
    from app.models import TxKind
    from app.services import accounts as acc
    from app.services import transactions as txs
    a = acc.create_account(session, name="HourPref")
    # Same day: daily close 90k, but the tx's 15-min candle was 95k.
    pricing.upsert(session, dt.date(2025, 1, 1), Decimal("90000"))
    pricing.upsert_hour(session, dt.datetime(2025, 1, 1, 15, 30), Decimal("95000"))  # 15m bucket
    buy = txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                              timestamp=dt.datetime(2025, 1, 1, 15, 37), amount_sats=txs.btc_to_sats("0.01"))
    res = pricing.backfill_prices(session, a.id)
    session.refresh(buy)
    assert buy.price_usd == Decimal("95000.00")        # 15m candle close, not the daily
    assert buy.fiat_value == Decimal("950.00")
    assert buy.fiat_source == "estimate"
    assert res.used_daily_fallback is False


def test_daily_fallback_when_no_hourly(session):
    from app.models import TxKind
    from app.services import accounts as acc
    from app.services import transactions as txs
    a = acc.create_account(session, name="DailyFallback")
    pricing.upsert(session, dt.date(2025, 1, 1), Decimal("90000"))  # only daily available
    buy = txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                              timestamp=dt.datetime(2025, 1, 1, 15, 30), amount_sats=txs.btc_to_sats("0.01"))
    res = pricing.backfill_prices(session, a.id)
    session.refresh(buy)
    assert buy.price_usd == Decimal("90000.00")
    assert res.used_daily_fallback is True


def test_backfill_never_overwrites_authoritative_value(session):
    from app.models import TxKind
    from app.services import accounts as acc
    from app.services import transactions as txs
    a = acc.create_account(session, name="Locked")
    pricing.upsert_hour(session, dt.datetime(2025, 1, 1, 15), Decimal("95000"))
    # User-entered value (manual) and an exchange value (actual) must both survive a backfill.
    manual = txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                                 timestamp=dt.datetime(2025, 1, 1, 15, 30), amount_sats=txs.btc_to_sats("0.01"),
                                 fiat_value=Decimal("1000.00"), fiat_source="manual")
    actual = txs.add_transaction(session, account_id=a.id, kind=TxKind.SELL,
                                 timestamp=dt.datetime(2025, 1, 1, 15, 45), amount_sats=txs.btc_to_sats("0.01"),
                                 fiat_value=Decimal("1234.56"), fiat_source="actual")
    pricing.backfill_prices(session, a.id)
    session.refresh(manual)
    session.refresh(actual)
    assert manual.fiat_value == Decimal("1000.00") and manual.fiat_source == "manual"
    assert actual.fiat_value == Decimal("1234.56") and actual.fiat_source == "actual"


def test_clearing_value_makes_tx_estimate_eligible(session):
    from app.models import TxKind
    from app.services import accounts as acc
    from app.services import transactions as txs
    a = acc.create_account(session, name="ReEstimate")
    pricing.upsert_hour(session, dt.datetime(2025, 1, 1, 15, 30), Decimal("95000"))  # 15m bucket
    buy = txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                              timestamp=dt.datetime(2025, 1, 1, 15, 37), amount_sats=txs.btc_to_sats("0.01"),
                              fiat_value=Decimal("1000.00"), fiat_source="manual")
    # Clearing the USD value resets provenance -> the feed may estimate it again.
    txs.update_transaction(session, buy.id, kind=TxKind.BUY, timestamp=buy.timestamp,
                           amount_sats=buy.amount_sats, fiat_value=None)
    session.refresh(buy)
    assert buy.fiat_source is None
    pricing.backfill_prices(session, a.id)
    session.refresh(buy)
    assert buy.fiat_value == Decimal("950.00") and buy.fiat_source == "estimate"
