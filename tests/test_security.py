"""Regression tests for the pre-release security/correctness audit:
owner-scoping (IDOR), session-token expiry/revocation, CSRF origin check, and the
per-wallet cost-basis double-count fix.
"""
import datetime as dt
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account, TxKind, User
from app.services import accounts as acc
from app.services import auth as auth_svc
from app.services import costbasis
from app.services import transactions as txs


@pytest.fixture
def clean_users():
    """Delete users + accounts afterward so other tests stay in open mode."""
    yield
    with SessionLocal() as s:
        for row in s.query(Account).all():
            s.delete(row)
        for row in s.query(User).all():
            s.delete(row)
        s.commit()
    auth_svc.get_secret_key.cache_clear()


# --- session tokens: expiry + revocation -------------------------------------
def test_token_expiry_and_version():
    tok = auth_svc.sign_token(7, token_version=3)
    decoded = auth_svc.decode_token(tok)
    assert decoded is not None and decoded[0] == 7 and decoded[2] == 3
    assert auth_svc.verify_token(tok) == 7
    # Expired (issued far in the past) -> rejected.
    old = auth_svc.sign_token(7, token_version=0, issued_at=int(time.time()) - 10**9)
    assert auth_svc.decode_token(old) is None
    assert auth_svc.verify_token(old) is None
    # Tampered signature -> rejected.
    assert auth_svc.decode_token(tok + "x") is None


# --- CSRF: cross-origin state change blocked ---------------------------------
def test_cross_origin_post_blocked(client):
    # Mismatched Origin on a POST is rejected; no account is created.
    r = client.post("/accounts", data={"name": "CsrfShouldFail"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    with SessionLocal() as s:
        assert s.query(Account).filter_by(name="CsrfShouldFail").first() is None


# --- IDOR: a member cannot reach another owner's account ---------------------
def test_member_cannot_access_other_owner_data(client, clean_users):
    from app.main import app

    client.post("/setup", data={"username": "admin", "password": "secret1"})
    client.post("/accounts", data={"name": "AdminAcct"})
    with SessionLocal() as s:
        admin_id = s.query(Account).filter_by(name="AdminAcct").first().id
        auth_svc.create_user(s, "kid", "pw123456", role="member")

    member = TestClient(app)
    member.post("/login", data={"username": "kid", "password": "pw123456"})

    # Full-page GET is redirected away (not 200 with the admin's data).
    assert member.get(f"/accounts/{admin_id}", follow_redirects=False).status_code == 303
    # Mutating POST is denied (404 — don't even reveal existence).
    assert member.post(f"/accounts/{admin_id}/transactions",
                       data={"kind": "buy", "amount_btc": "0.1"}).status_code == 404
    # Tax export of someone else's account is denied.
    assert member.get(f"/tax/{admin_id}/8949", follow_redirects=False).status_code == 303
    assert member.get(f"/tax/{admin_id}/8949.csv", follow_redirects=False).status_code == 303


# --- reconciliation must not cross user boundaries ---------------------------
def test_reconcile_does_not_cross_user_boundary(session):
    # Two different app users, both with blank owner labels, sharing a txid. Reconciliation
    # must NOT treat them as the same owner (that would let one user mutate another's basis).
    a1 = acc.create_account(session, name="UserOneAcct", owner_user_id=1)
    a2 = acc.create_account(session, name="UserTwoAcct", owner_user_id=2)
    half = txs.btc_to_sats("0.5")
    txs.add_transaction(session, account_id=a1.id, kind=TxKind.BUY, timestamp=dt.datetime(2025, 1, 1),
                        amount_sats=half, fiat_value=Decimal("30000"))
    txs.add_transaction(session, account_id=a1.id, kind=TxKind.TRANSFER_OUT, timestamp=dt.datetime(2025, 2, 1),
                        amount_sats=half, txid="SHARED", external_id="SHARED:out")
    txs.add_transaction(session, account_id=a2.id, kind=TxKind.TRANSFER_IN, timestamp=dt.datetime(2025, 2, 1),
                        amount_sats=half, txid="SHARED", external_id="SHARED:in")
    assert costbasis.reconcile_internal_transfers(session) == 0          # no cross-user carry
    assert costbasis.compute_account(session, a2.id).holding_basis_usd == Decimal("0.00")


# --- cost basis: intra-account transfer must not double-count ----------------
def test_per_wallet_basis_consistent_with_account(session):
    a = acc.create_account(session, name="Multi")
    w1 = acc.add_wallet(session, a.id, "Hot", "xpub", xpub=None)
    w2 = acc.add_wallet(session, a.id, "Cold", "xpub", xpub=None)
    # Buy in w1, then move the coins w1 -> w2 on one on-chain tx (shared txid, same account).
    txs.add_transaction(session, account_id=a.id, wallet_id=w1.id, kind=TxKind.BUY,
                        timestamp=dt.datetime(2024, 1, 1), amount_sats=txs.btc_to_sats("0.1"),
                        fiat_value=Decimal("3000.00"))
    txs.add_transaction(session, account_id=a.id, wallet_id=w1.id, kind=TxKind.TRANSFER_OUT,
                        timestamp=dt.datetime(2024, 2, 1), amount_sats=txs.btc_to_sats("0.1"), txid="move1")
    txs.add_transaction(session, account_id=a.id, wallet_id=w2.id, kind=TxKind.TRANSFER_IN,
                        timestamp=dt.datetime(2024, 2, 1), amount_sats=txs.btc_to_sats("0.1"), txid="move1")

    account_res, per_wallet = costbasis.compute_account_breakdown(session, a.id)
    # Account view: the move is internal churn -> 0.1 BTC held, $3000 basis preserved.
    assert account_res.holding_sats == txs.btc_to_sats("0.1")
    assert account_res.holding_basis_usd == Decimal("3000.00")
    # Per-wallet basis/holdings must sum to the account total (the old bug summed to $0 basis).
    assert sum(r.holding_sats for _, r in per_wallet) == account_res.holding_sats
    assert sum((r.holding_basis_usd for _, r in per_wallet), Decimal("0")) == Decimal("3000.00")
