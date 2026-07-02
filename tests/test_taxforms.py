"""Phase 5: Form 8949 / Schedule D generation + routes."""
import datetime as dt
from decimal import Decimal

from app.models import SATS_PER_BTC
from app.services import taxforms
from app.services.costbasis import CostBasisResult, Disposal


def _result():
    return CostBasisResult(disposals=[
        Disposal(date=dt.datetime(2025, 3, 1), kind="sell", sats=int(Decimal("0.5") * SATS_PER_BTC),
                 proceeds_usd=Decimal("20000.00"), basis_usd=Decimal("15000.00"),
                 acquired=dt.datetime(2025, 1, 1), term="short"),
        Disposal(date=dt.datetime(2025, 7, 1), kind="sell", sats=int(SATS_PER_BTC),
                 proceeds_usd=Decimal("60000.00"), basis_usd=Decimal("40000.00"),
                 acquired=dt.datetime(2024, 1, 1), term="long"),
    ])


def test_build_rows_and_totals():
    rows = taxforms.build_rows(_result())
    assert len(rows) == 2
    t = taxforms.totals(rows)
    assert t["short"]["gain"] == Decimal("5000.00")
    assert t["long"]["gain"] == Decimal("20000.00")
    assert t["net_gain"] == Decimal("25000.00")


def test_year_filter():
    assert len(taxforms.build_rows(_result(), year=2025)) == 2
    assert len(taxforms.build_rows(_result(), year=2024)) == 0
    assert taxforms.years_present(_result()) == [2025]


def test_csv_contains_schedule_d():
    csv_text = taxforms.to_csv(taxforms.build_rows(_result()), "Test", 2025)
    assert "Form 8949 — Test — 2025" in csv_text
    assert "Schedule D — short-term total" in csv_text
    assert "25000.00" in csv_text  # net gain


def test_readiness_flags():
    from app.models import Transaction, TxKind
    from app.services.costbasis import CostBasisResult
    txs = [
        # taxable sell with no USD value -> "warn"
        Transaction(id=1, account_id=1, kind=TxKind.SELL, timestamp=dt.datetime(2025, 1, 1),
                    amount_sats=1000, fiat_value=None),
        # a feed estimate -> "info"
        Transaction(id=2, account_id=1, kind=TxKind.BUY, timestamp=dt.datetime(2025, 1, 2),
                    amount_sats=1000, fiat_value=Decimal("100"), fiat_source="estimate"),
        # an actual value -> contributes no flag
        Transaction(id=3, account_id=1, kind=TxKind.BUY, timestamp=dt.datetime(2025, 1, 3),
                    amount_sats=1000, fiat_value=Decimal("100"), fiat_source="actual"),
    ]
    cb = CostBasisResult(warnings=["transfer in on 2025-01-01 has no cost basis"])
    flags = taxforms.readiness_flags(txs, cb, price_source="coinbase", unreconciled=2)
    msgs = " ".join(f.message for f in flags)
    assert "no USD value" in msgs           # missing-price flag
    assert "ESTIMATES" in msgs              # estimate flag (with source)
    assert "unmatched self-transfer" in msgs  # reconciliation flag
    assert "no cost basis" in msgs          # engine warning passed through
    # Clean data with nothing outstanding -> no flags at all.
    assert taxforms.readiness_flags([txs[2]], CostBasisResult(), price_source="coinbase") == []


def test_8949_routes(client):
    import re
    client.post("/accounts", data={"name": "TaxAcct"})
    aid = re.search(r"/accounts/(\d+)", client.get("/accounts").text).group(1)
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "buy", "timestamp": "2025-01-01", "amount_btc": "1.0", "fiat_value": "30000"})
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "sell", "timestamp": "2025-03-01", "amount_btc": "0.5", "fiat_value": "20000"})
    html = client.get(f"/tax/{aid}/8949?year=2025")
    assert html.status_code == 200
    assert "Form 8949" in html.text
    assert "5,000" in html.text  # short-term gain

    csv = client.get(f"/tax/{aid}/8949.csv?year=2025")
    assert csv.status_code == 200
    assert "text/csv" in csv.headers["content-type"]
    assert "attachment" in csv.headers["content-disposition"]
    assert "Schedule D" in csv.text


def test_zero_proceeds_disposal_is_flagged():
    import datetime as dt
    from decimal import Decimal
    from app.models import Transaction, TxKind
    from app.services import taxforms
    from app.services.costbasis import CostBasisResult
    # A sell recorded with $0 proceeds (not None) -> phantom loss; must be flagged.
    txs = [Transaction(kind=TxKind.SELL, timestamp=dt.datetime(2024, 7, 4), amount_sats=3_000_000,
                       fiat_value=Decimal("0"), fiat_source="actual")]
    msgs = [f.message for f in taxforms.readiness_flags(txs, CostBasisResult())]
    assert any("$0 proceeds" in m for m in msgs)
    # A normal-priced sell is NOT flagged for zero proceeds.
    ok = [Transaction(kind=TxKind.SELL, timestamp=dt.datetime(2024, 7, 4), amount_sats=3_000_000,
                      fiat_value=Decimal("1800"), fiat_source="actual")]
    assert not any("$0 proceeds" in f.message for f in taxforms.readiness_flags(ok, CostBasisResult()))
