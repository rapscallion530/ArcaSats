"""Unified CSV->DB mapping: raw-row stash, custodian-provided basis/acquisition-date (transfer-in),
own-address auto-linkage, and the rich transaction detail view."""
import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, Transaction, TxKind, Utxo
from app.services import accounts as acc
from app.services import costbasis
from app.services import transactions as txsvc
from app.services.importers import csv_import


def test_swan_deposit_with_cost_basis_becomes_transfer_in(session):
    # A Swan BTC deposit tagged with USD Cost Basis + Acquisition Date is a transfer-IN of coins
    # you already owned: carry the basis and back-date the lot (not a buy at deposit time).
    a = acc.create_account(session, name="SwanAcct", label_kind="KYC")
    csv = ("Event,Date,Timezone,Status,Transaction ID,Total USD,Transaction USD,Fee USD,Unit Count,"
           "Asset Type,BTC Price,Address Label,USD Cost Basis,Acquisition Date\n"
           "deposit,2025-03-01 09:00:00+00,UTC,settled,,,,,0.50000000,BTC,,Transferred in,12000,2021-05-01\n")
    csv_import.import_csv(session, account_id=a.id, source="swan", text=csv)
    tx = session.scalar(select(Transaction).where(Transaction.account_id == a.id))
    assert tx.kind == TxKind.TRANSFER_IN
    assert tx.carried_basis_usd == Decimal("12000.00")
    assert tx.acquired_at == dt.datetime(2021, 5, 1)
    assert tx.raw_fields.get("address label") == "Transferred in"   # raw row captured losslessly
    cb = costbasis.compute_account(session, a.id)
    assert cb.holding_basis_usd == Decimal("12000.00")              # custodian basis is authoritative
    assert cb.open_lots[0].acquired.year == 2021                    # lot back-dated to acquisition


def test_acquired_at_backdates_holding_period(session):
    a = acc.create_account(session, name="Backdate")
    txsvc.add_transaction(session, account_id=a.id, kind=TxKind.BUY, timestamp=dt.datetime(2025, 1, 1),
                          amount_sats=100_000_000, fiat_value=Decimal("40000"),
                          acquired_at=dt.datetime(2023, 1, 1))
    txsvc.add_transaction(session, account_id=a.id, kind=TxKind.SELL, timestamp=dt.datetime(2025, 2, 1),
                          amount_sats=100_000_000, fiat_value=Decimal("60000"))
    cb = costbasis.compute_account(session, a.id)
    # Bought (per acquired_at) 2023-01-01, sold 2025-02-01 -> LONG term despite the 2025 event date.
    assert cb.disposals[0].term == "long"


def test_csv_withdrawal_to_own_address_auto_links(session):
    # A CSV Send whose destination address is one of our own received addresses (a Utxo) is
    # provably a self-transfer: auto-stamp the receive txid, relabel both sides, carry basis.
    cold = acc.create_account(session, name="Cold")
    wc = acc.add_wallet(session, cold.id, "wc", "xpub", xpub=None)
    txsvc.add_transaction(session, account_id=cold.id, wallet_id=wc.id, kind=TxKind.BUY,
                          timestamp=dt.datetime(2025, 2, 1), amount_sats=50_000_000, txid="RX",
                          external_id="RX:in", source=f"xpub:{wc.id}")
    session.add(Utxo(account_id=cold.id, wallet_id=wc.id, txid="RX", vout=0,
                     value_sats=50_000_000, address="bc1q-mine"))
    session.commit()
    swan = acc.create_account(session, name="Swan")
    txsvc.add_transaction(session, account_id=swan.id, kind=TxKind.BUY, timestamp=dt.datetime(2025, 1, 1),
                          amount_sats=50_000_000, fiat_value=Decimal("20000"))
    sell = txsvc.add_transaction(session, account_id=swan.id, kind=TxKind.SELL,
                                 timestamp=dt.datetime(2025, 2, 1), amount_sats=50_000_000,
                                 address="bc1q-mine", source="csv:swan", external_id="swan-send")
    costbasis.reconcile_internal_transfers(session)
    session.refresh(sell)
    assert sell.kind == TxKind.TRANSFER_OUT and sell.txid == "RX"   # stamped from the owned Utxo
    cold_in = session.scalar(select(Transaction).where(
        Transaction.account_id == cold.id, Transaction.txid == "RX"))
    assert cold_in.kind == TxKind.TRANSFER_IN
    assert cold_in.carried_basis_usd == Decimal("20000.00")        # basis carried Swan -> Cold


def test_swan_withdrawal_completed_status_is_imported(session):
    # Regression: a real Swan withdrawals export marks executed rows "Completed", not "settled".
    # A strict ==\"settled\" filter dropped them all (inflating the balance). Keep executed,
    # drop only canceled.
    a = acc.create_account(session, name="SwanW")
    csv = ("Created At,Timezone,Transaction ID,Executed At,Canceled At,Status,Bitcoin Amount,Automatic,IP Address\n"
           "2025-01-15 12:00:00+00,UTC,abc123,2025-01-15 13:00:00+00,,Completed,0.00500000,t,10.0.0.1\n"
           "2025-02-20 12:00:00+00,UTC,,,2025-02-22 00:00:00+00,primetrust-canceled,0.01000000,t,10.0.0.1\n")
    csv_import.import_csv(session, account_id=a.id, source="swan", text=csv)
    txs = session.scalars(select(Transaction).where(Transaction.account_id == a.id)).all()
    assert len(txs) == 1                                   # Completed kept; canceled dropped
    assert txs[0].kind == TxKind.SELL and txs[0].txid == "abc123"


def test_coinbase_send_captures_recipient_address(session):
    # Coinbase's "Recipient Address" is now mapped to the tx address so a withdrawal to your own
    # wallet can auto-link (address-based reconciliation), not just live in the raw stash.
    a = acc.create_account(session, name="CbAddr")
    csv = ("ID,Timestamp,Transaction Type,Asset,Quantity Transacted,Price Currency,"
           "Price at Transaction,Subtotal,Total (inclusive of fees and/or spread),"
           "Fees and/or Spread,Notes,Sender Address,Recipient Address\n"
           "x1,2025-01-02 10:00:00 UTC,Send,BTC,-0.01000000,USD,90000,,,,,,bc1q-mycold\n")
    csv_import.import_csv(session, account_id=a.id, source="coinbase", text=csv)
    tx = session.scalar(select(Transaction).where(Transaction.account_id == a.id))
    assert tx.kind == TxKind.SELL and tx.address == "bc1q-mycold"


def _aid(name: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Account.id).where(Account.name == name))


def test_detail_view_shows_all_fields_and_raw_row(client):
    client.post("/accounts", data={"name": "DetailAcct"})
    aid = _aid("DetailAcct")
    csv = "date,type,amount_btc,usd_value,txid,address,memo\n2025-01-01,buy,0.01,900,abcd1234,bc1qexampleaddr,hello note\n"
    client.post(f"/accounts/{aid}/import/csv",
                data={"source": "generic"},
                files={"file": ("g.csv", csv.encode(), "text/csv")})
    import re
    tid = re.search(rf"/accounts/{aid}/transactions/(\d+)/edit-form",
                    client.get(f"/accounts/{aid}").text).group(1)
    panel = client.get(f"/accounts/{aid}/transactions/{tid}/edit-form").text
    assert "Original CSV row" in panel        # raw stash surfaced
    assert "abcd1234" in panel and "bc1qexampleaddr" in panel   # txid + address shown/editable
    assert "Time (UTC)" in panel              # exact-time detail


def test_detail_edit_sets_txid_and_runs_linkage(client):
    client.post("/accounts", data={"name": "EditLink"})
    aid = _aid("EditLink")
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "sell", "timestamp": "2025-03-01", "amount_btc": "0.1", "fiat_value": "6000"})
    import re
    tid = re.search(rf"/accounts/{aid}/transactions/(\d+)/edit-form",
                    client.get(f"/accounts/{aid}").text).group(1)
    r = client.post(f"/accounts/{aid}/transactions/{tid}/edit",
                    data={"kind": "sell", "timestamp": "2025-03-01T10:30", "amount_btc": "0.1",
                          "fiat_value": "6000", "txid": "ffee1122", "address": "", "acquired_at": "",
                          "cost_basis": ""})
    assert r.status_code == 200
    with SessionLocal() as s:
        tx = s.get(Transaction, int(tid))
        assert tx.txid == "ffee1122"
        assert tx.timestamp == dt.datetime(2025, 3, 1, 10, 30)   # exact time persisted
