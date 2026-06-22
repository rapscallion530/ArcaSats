"""Phase 2: CSV import for each source + dedupe + route."""
from pathlib import Path

from app.models import TxKind
from app.services import accounts as acc
from app.services import transactions as tx_svc
from app.services.importers import csv_import

FIX = Path(__file__).parent / "fixtures"


def _import(session, source, fname):
    a = acc.create_account(session, name=f"acct-{source}")
    text = (FIX / fname).read_text(encoding="utf-8")
    return a, csv_import.import_csv(session, account_id=a.id, source=source, text=text)


def test_coinbase_import_filters_non_btc_and_maps_kinds(session):
    a, r = _import(session, "coinbase", "coinbase_sample.csv")
    assert r.errors == []
    assert r.imported == 5  # 6 rows, ETH row ignored
    txs = tx_svc.list_transactions(session, a.id)
    kinds = {t.kind for t in txs}
    assert kinds == {TxKind.BUY, TxKind.INCOME, TxKind.SELL, TxKind.TRANSFER_IN, TxKind.TRANSFER_OUT}
    buy = next(t for t in txs if t.kind == TxKind.BUY)
    assert buy.amount_sats == 1_000_000  # 0.01 BTC
    assert str(buy.fiat_value) == "910.00"


def test_bad_rows_are_rejected_not_silently_coerced(session):
    a = acc.create_account(session, name="acct-reject")
    text = ("type,date,amount_btc,usd_value\n"
            "buy,2025-01-01,0.1,3000\n"
            "buy,not-a-date,0.2,6000\n"      # unparseable date -> rejected (was silently 1970)
            "sell,2025-02-01,notanumber,0\n")  # zero/invalid amount -> rejected
    r = csv_import.import_csv(session, account_id=a.id, source="generic", text=text)
    assert r.imported == 1
    assert len(r.rejected) >= 2
    assert any("date" in msg for msg in r.rejected)
    # The good row landed with the right year (not a 1970 sentinel).
    txs = tx_svc.list_transactions(session, a.id)
    assert len(txs) == 1 and txs[0].timestamp.year == 2025


def test_offset_timestamp_converts_to_utc(session):
    import datetime as dt
    a = acc.create_account(session, name="acct-tz")
    # 00:30 at +05:00 is 19:30 the PREVIOUS day in UTC — must shift the date (and year).
    text = "type,date,amount_btc,usd_value\nbuy,2025-01-01T00:30:00+05:00,0.1,3000\n"
    r = csv_import.import_csv(session, account_id=a.id, source="generic", text=text)
    assert r.imported == 1
    t = tx_svc.list_transactions(session, a.id)[0]
    assert t.timestamp == dt.datetime(2024, 12, 31, 19, 30, 0)


def test_dedup_is_account_scoped(session):
    a1 = acc.create_account(session, name="acct-d1")
    a2 = acc.create_account(session, name="acct-d2")
    text = "type,date,amount_btc,usd_value,external_id\nbuy,2025-01-01,0.1,3000,ROW1\n"
    r1 = csv_import.import_csv(session, account_id=a1.id, source="generic", text=text)
    r2 = csv_import.import_csv(session, account_id=a2.id, source="generic", text=text)
    # Same external id imported into DIFFERENT accounts -> both kept (not cross-account dedup).
    assert r1.imported == 1 and r2.imported == 1


def test_reimport_is_idempotent(session):
    a, r1 = _import(session, "coinbase", "coinbase_sample.csv")
    text = (FIX / "coinbase_sample.csv").read_text(encoding="utf-8")
    r2 = csv_import.import_csv(session, account_id=a.id, source="coinbase", text=text)
    assert r2.imported == 0
    assert r2.skipped == 5


def test_strike_import(session):
    a, r = _import(session, "strike", "strike_sample.csv")
    assert r.imported == 3 and r.errors == []
    txs = tx_svc.list_transactions(session, a.id)
    assert any(t.kind == TxKind.BUY for t in txs)
    assert any(t.kind == TxKind.TRANSFER_OUT for t in txs)


def test_swan_import(session):
    a, r = _import(session, "swan", "swan_sample.csv")
    assert r.imported == 3 and r.errors == []


def test_bisq_import(session):
    a, r = _import(session, "bisq", "bisq_sample.csv")
    assert r.imported == 2 and r.errors == []
    txs = tx_svc.list_transactions(session, a.id)
    assert {t.kind for t in txs} == {TxKind.BUY, TxKind.SELL}


def test_generic_import(session):
    a, r = _import(session, "generic", "generic_sample.csv")
    assert r.imported == 3 and r.errors == []


def test_unknown_source(session):
    a = acc.create_account(session, name="x")
    r = csv_import.import_csv(session, account_id=a.id, source="nope", text="a,b\n1,2")
    assert r.imported == 0 and r.errors


def test_import_route(client):
    client.post("/accounts", data={"name": "ImportAcct"})
    import re
    aid = re.search(r"/accounts/(\d+)", client.get("/accounts").text).group(1)
    text = (FIX / "generic_sample.csv").read_text(encoding="utf-8")
    r = client.post(f"/accounts/{aid}/import/csv",
                    data={"source": "generic"},
                    files={"file": ("g.csv", text, "text/csv")})
    assert r.status_code == 200
    assert "Imported" in r.text
