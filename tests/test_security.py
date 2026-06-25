"""Security/correctness regression tests: the CSRF same-origin check and the per-wallet
cost-basis double-count fix. (Multi-user IDOR/session tests were retired when the app became
single-user — see test_auth.py for the optional password lock.)
"""
import datetime as dt
from decimal import Decimal

from app.db import SessionLocal
from app.models import Account, TxKind
from app.services import accounts as acc
from app.services import costbasis
from app.services import transactions as txs


# --- CSRF: cross-origin state change blocked ---------------------------------
def test_cross_origin_post_blocked(client):
    # Mismatched Origin on a POST is rejected; no account is created.
    r = client.post("/accounts", data={"name": "CsrfShouldFail"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    with SessionLocal() as s:
        assert s.query(Account).filter_by(name="CsrfShouldFail").first() is None


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
