"""Reconciliation inbox: suggest / confirm / reject no-shared-txid self-transfers."""
import datetime as dt
from decimal import Decimal

from app.models import TxKind
from app.services import accounts as acc
from app.services import costbasis
from app.services import transactions as tx_svc


def _two_hop(session, *, owner_c="", out_txid=None, in_txid=None):
    """A: buy 0.01 then send 0.01 out; C: receive 0.0099 the next day (a hop we don't track)."""
    a = acc.create_account(session, name="A")
    c = acc.create_account(session, name="C", owner=owner_c)
    tx_svc.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                           timestamp=dt.datetime(2025, 1, 1), amount_sats=1_000_000,
                           fiat_value=Decimal("1000"), fiat_source="actual")
    out_tx = tx_svc.add_transaction(session, account_id=a.id, kind=TxKind.SELL,
                                    timestamp=dt.datetime(2025, 1, 5), amount_sats=1_000_000,
                                    fiat_value=Decimal("1100"), fiat_source="actual", txid=out_txid)
    in_tx = tx_svc.add_transaction(session, account_id=c.id, kind=TxKind.BUY,
                                   timestamp=dt.datetime(2025, 1, 6), amount_sats=990_000,
                                   fiat_value=Decimal("1050"), fiat_source="actual", txid=in_txid)
    return a, c, out_tx, in_tx


def test_suggests_sell_buy_pair_across_accounts(session):
    a, c, out_tx, in_tx = _two_hop(session)
    sugg = costbasis.suggest_transfers(session)
    assert len(sugg) == 1
    s = sugg[0]
    assert s.out_tx.id == out_tx.id and s.in_tx.id == in_tx.id
    assert s.amount_delta_sats == 10_000 and s.days_apart == 1
    assert s.out_account == "A" and s.in_account == "C"


def test_shared_txid_pair_is_not_suggested(session):
    # Same txid would be handled by the automatic reconciler, not the inbox.
    _two_hop(session, out_txid="deadbeef", in_txid="deadbeef")
    assert costbasis.suggest_transfers(session) == []


def test_different_owner_not_suggested(session):
    _two_hop(session, owner_c="Alice")     # C belongs to a different owner -> gift, not transfer
    assert costbasis.suggest_transfers(session) == []


def test_amount_out_of_tolerance_not_suggested(session):
    a = acc.create_account(session, name="A")
    c = acc.create_account(session, name="C")
    tx_svc.add_transaction(session, account_id=a.id, kind=TxKind.SELL,
                           timestamp=dt.datetime(2025, 1, 5), amount_sats=1_000_000,
                           fiat_value=Decimal("1100"))
    tx_svc.add_transaction(session, account_id=c.id, kind=TxKind.BUY,
                           timestamp=dt.datetime(2025, 1, 6), amount_sats=500_000,  # way off
                           fiat_value=Decimal("550"))
    assert costbasis.suggest_transfers(session) == []


def test_confirm_relabels_and_carries_basis(session):
    a, c, out_tx, in_tx = _two_hop(session)
    ok, err = costbasis.confirm_transfer(session, out_tx.id, in_tx.id)
    assert ok and err == ""
    session.refresh(out_tx)
    session.refresh(in_tx)
    assert out_tx.kind == TxKind.TRANSFER_OUT and in_tx.kind == TxKind.TRANSFER_IN
    # basis of the source lot ($1000) carried onto the destination transfer_in
    assert in_tx.carried_basis_usd == Decimal("1000.00")
    # both reviewed -> the pair leaves the queue
    assert costbasis.suggest_transfers(session) == []


def test_reject_marks_reviewed_and_keeps_kinds(session):
    a, c, out_tx, in_tx = _two_hop(session)
    ok, _ = costbasis.reject_suggestion(session, out_tx.id, in_tx.id)
    assert ok
    session.refresh(out_tx)
    assert out_tx.kind == TxKind.SELL            # unchanged — genuinely external
    assert costbasis.suggest_transfers(session) == []


def test_confirm_rejects_cross_owner(session):
    a, c, out_tx, in_tx = _two_hop(session, owner_c="Bob")
    ok, err = costbasis.confirm_transfer(session, out_tx.id, in_tx.id)
    assert not ok and "owner" in err


def test_reconcile_route_and_confirm(client):
    import re
    from app.db import SessionLocal
    client.post("/accounts", data={"name": "RA"})
    client.post("/accounts", data={"name": "RC"})
    with SessionLocal() as s:
        from app.models import Account
        from sqlalchemy import select
        aid = s.scalar(select(Account.id).where(Account.name == "RA"))
        cid = s.scalar(select(Account.id).where(Account.name == "RC"))
        out_tx = tx_svc.add_transaction(s, account_id=aid, kind=TxKind.SELL,
                                        timestamp=dt.datetime(2025, 1, 5), amount_sats=1_000_000,
                                        fiat_value=Decimal("1100"))
        in_tx = tx_svc.add_transaction(s, account_id=cid, kind=TxKind.BUY,
                                       timestamp=dt.datetime(2025, 1, 6), amount_sats=995_000,
                                       fiat_value=Decimal("1090"))
        out_id, in_id = out_tx.id, in_tx.id

    page = client.get("/reconcile")
    assert page.status_code == 200 and "Reconciliation inbox" in page.text
    assert "RA" in page.text and "RC" in page.text

    r = client.post("/reconcile/confirm", data={"out_tx_id": out_id, "in_tx_id": in_id})
    assert r.status_code == 200
    with SessionLocal() as s:
        from app.models import Transaction
        assert s.get(Transaction, out_id).kind == TxKind.TRANSFER_OUT
        assert s.get(Transaction, in_id).kind == TxKind.TRANSFER_IN
