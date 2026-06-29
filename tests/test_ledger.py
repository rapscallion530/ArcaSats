"""Master ledger: all transactions across accounts/wallets in one view, with filters + CSV."""
import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, TxKind
from app.services import transactions as txsvc


def _seed(client, name: str, kind: str, when: dt.datetime, sats: int) -> int:
    client.post("/accounts", data={"name": name, "label_kind": "KYC"})
    with SessionLocal() as s:
        aid = s.scalar(select(Account.id).where(Account.name == name))
        txsvc.add_transaction(s, account_id=aid, kind=kind, timestamp=when,
                              amount_sats=sats, fiat_value=Decimal("50"))
    return aid


def test_ledger_shows_all_accounts_together(client):
    _seed(client, "LedgerA", TxKind.BUY, dt.datetime(2024, 1, 1), 100_000)
    _seed(client, "LedgerB", TxKind.SELL, dt.datetime(2025, 2, 2), 40_000)
    html = client.get("/ledger").text
    assert "Master ledger" in html
    assert "LedgerA" in html and "LedgerB" in html     # both accounts in one unified table


def test_ledger_csv_has_header_and_rows(client):
    _seed(client, "LedgerCsv", TxKind.BUY, dt.datetime(2024, 3, 3), 12_345)
    r = client.get("/ledger.csv")
    assert r.status_code == 200
    assert "Date (UTC),Account,Wallet,Type,BTC,USD value,KYC,Counterparty,Txid" in r.text
    assert "LedgerCsv" in r.text


def test_ledger_empty_filters_do_not_422(client):
    # The onchange form submits empty strings for the "All …" options alongside a real pick;
    # empty values must be treated as no filter, not rejected as a bad int.
    a = _seed(client, "EmptyFilt", TxKind.BUY, dt.datetime(2024, 6, 6), 5_000)
    assert client.get(f"/ledger?account_id={a}&kind=&year=").status_code == 200   # pick acct, rest "All"
    assert client.get("/ledger?account_id=&kind=&year=").status_code == 200       # all "All"
    assert client.get("/ledger.csv?account_id=&kind=&year=").status_code == 200


def test_ledger_csv_filters_by_account(client):
    a = _seed(client, "OnlyA", TxKind.BUY, dt.datetime(2024, 4, 4), 1_000)
    _seed(client, "OnlyB", TxKind.SELL, dt.datetime(2024, 4, 5), 2_000)
    text = client.get(f"/ledger.csv?account_id={a}").text
    assert "OnlyA" in text and "OnlyB" not in text       # CSV respects the account filter
