"""Combined tax report: one 8949/Schedule D aggregating every account's per-account disposals
(sum of per-account results, not cross-account lot re-pooling)."""
import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, TxKind
from app.services import taxforms
from app.services import transactions as txsvc


def _seed_sale(client, name, buy_usd, sell_usd, sold):
    client.post("/accounts", data={"name": name, "label_kind": "KYC"})
    with SessionLocal() as s:
        aid = s.scalar(select(Account.id).where(Account.name == name))
        txsvc.add_transaction(s, account_id=aid, kind=TxKind.BUY, timestamp=sold - dt.timedelta(days=400),
                              amount_sats=100_000_000, fiat_value=Decimal(buy_usd))
        txsvc.add_transaction(s, account_id=aid, kind=TxKind.SELL, timestamp=sold,
                              amount_sats=100_000_000, fiat_value=Decimal(sell_usd))
    return aid


def test_totals_by_account_unit():
    rows = [
        taxforms.Form8949Row("1 BTC", dt.datetime(2023, 1, 1), dt.datetime(2024, 1, 1),
                             Decimal("15000"), Decimal("10000"), "long", account="A"),
        taxforms.Form8949Row("1 BTC", dt.datetime(2023, 1, 1), dt.datetime(2024, 1, 1),
                             Decimal("12000"), Decimal("20000"), "short", account="B"),
    ]
    by = taxforms.totals_by_account(rows)
    assert by["A"]["total"] == Decimal("5000.00") and by["A"]["long"] == Decimal("5000.00")
    assert by["B"]["total"] == Decimal("-8000.00") and by["B"]["count"] == 1


def test_combined_report_lists_all_accounts(client):
    _seed_sale(client, "TaxAlpha", "10000", "15000", dt.datetime(2024, 6, 1))
    _seed_sale(client, "TaxBravo", "20000", "12000", dt.datetime(2024, 6, 1))
    html = client.get("/tax/combined?year=2024").text
    assert "All accounts (combined)" in html
    assert "TaxAlpha" in html and "TaxBravo" in html and "By account" in html


def test_combined_csv_has_account_column(client):
    _seed_sale(client, "TaxCsv", "5000", "9000", dt.datetime(2023, 3, 3))
    r = client.get("/tax/combined.csv?year=2023")
    assert r.status_code == 200
    assert "Account,Part,Description" in r.text and "TaxCsv" in r.text
