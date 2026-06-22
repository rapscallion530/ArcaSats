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
