"""Phase 1: accounts, wallets, manual transactions, balances."""
import datetime as dt
from decimal import Decimal

from app.models import TxKind
from app.services import accounts as acc
from app.services import transactions as txs


def test_create_and_list_account(session):
    a = acc.create_account(session, name="Personal (KYC)", label_kind="KYC")
    assert a.id is not None
    listed = acc.list_accounts(session)
    assert len(listed) == 1
    assert listed[0].name == "Personal (KYC)"


def test_btc_to_sats_no_float_drift():
    assert txs.btc_to_sats("0.1") == 10_000_000
    assert txs.btc_to_sats("0.00000001") == 1
    assert txs.btc_to_sats("1") == 100_000_000
    # 0.1+0.2 style drift must not appear
    assert txs.btc_to_sats("0.3") == 30_000_000


def test_balance_inflows_minus_outflows(session):
    a = acc.create_account(session, name="A")
    ts = dt.datetime(2025, 1, 1)
    txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY, timestamp=ts,
                        amount_sats=txs.btc_to_sats("0.5"), fiat_value=Decimal("30000"))
    txs.add_transaction(session, account_id=a.id, kind=TxKind.SELL, timestamp=ts,
                        amount_sats=txs.btc_to_sats("0.2"), fiat_value=Decimal("14000"))
    txs.add_transaction(session, account_id=a.id, kind=TxKind.TRANSFER_IN, timestamp=ts,
                        amount_sats=txs.btc_to_sats("0.1"))
    # 0.5 - 0.2 + 0.1 = 0.4
    assert acc.balance_sats(session, a.id) == txs.btc_to_sats("0.4")


def test_price_derived_from_fiat_and_amount(session):
    a = acc.create_account(session, name="A")
    tx = txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                             timestamp=dt.datetime(2025, 1, 1),
                             amount_sats=txs.btc_to_sats("0.5"), fiat_value=Decimal("30000"))
    # price per BTC should be 60000
    assert tx.price_usd == Decimal("60000.00")


def test_duplicate_import_is_skipped(session):
    a = acc.create_account(session, name="A")
    ts = dt.datetime(2025, 1, 1)
    first = txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY, timestamp=ts,
                                amount_sats=1000, source="csv:test", external_id="abc")
    dup = txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY, timestamp=ts,
                              amount_sats=1000, source="csv:test", external_id="abc")
    assert first is not None
    assert dup is None  # dedupe via UniqueConstraint(source, external_id)


def test_account_crud_routes(client):
    # create
    r = client.post("/accounts", data={"name": "RouteAcct", "label_kind": "non-KYC"})
    assert r.status_code == 200
    assert "RouteAcct" in r.text
    # it appears on the accounts page
    r2 = client.get("/accounts")
    assert "RouteAcct" in r2.text


def test_add_transaction_route(client):
    client.post("/accounts", data={"name": "TxAcct"})
    # find its id from the accounts page link
    page = client.get("/accounts").text
    import re
    m = re.search(r"/accounts/(\d+)", page)
    assert m
    aid = m.group(1)
    r = client.post(f"/accounts/{aid}/transactions", data={
        "kind": "buy", "timestamp": "2025-03-01", "amount_btc": "0.01", "fiat_value": "650",
    })
    assert r.status_code == 200
    assert "0.01000000" in r.text
