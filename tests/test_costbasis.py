"""Phase 4: FIFO cost-basis engine."""
import datetime as dt
from decimal import Decimal

from app.models import SATS_PER_BTC, Transaction, TxKind
from app.services import costbasis


def tx(i, kind, date, btc, usd=None, price=None, fee_usd=None, wallet_id=None):
    return Transaction(
        id=i, account_id=1, wallet_id=wallet_id, kind=kind,
        timestamp=dt.datetime.fromisoformat(date),
        amount_sats=int((Decimal(str(btc)) * SATS_PER_BTC)),
        fiat_value=Decimal(str(usd)) if usd is not None else None,
        price_usd=Decimal(str(price)) if price is not None else None,
        fiat_fee=Decimal(str(fee_usd)) if fee_usd is not None else None,
    )


def test_simple_buy_sell_short_term():
    txs = [
        tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="30000"),
        tx(2, TxKind.SELL, "2025-03-01", "0.5", usd="20000"),
    ]
    r = costbasis.compute(txs)
    assert len(r.disposals) == 1
    d = r.disposals[0]
    assert d.basis_usd == Decimal("15000.00")     # half of 30000
    assert d.proceeds_usd == Decimal("20000.00")
    assert d.gain_usd == Decimal("5000.00")
    assert d.term == "short"
    assert r.holding_sats == int(Decimal("0.5") * SATS_PER_BTC)
    assert r.holding_basis_usd == Decimal("15000.00")
    assert r.realized_short_usd == Decimal("5000.00")
    assert r.realized_long_usd == Decimal("0.00")


def test_avg_cost_per_unit_usd():
    # Buy 1 BTC @ $30k, sell 0.5 -> hold 0.5 BTC with $15k basis. Per-unit = basis/qty = $30k/BTC
    # (the original acquisition price), NOT the $15k total basis.
    r = costbasis.compute([
        tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="30000"),
        tx(2, TxKind.SELL, "2025-03-01", "0.5", usd="20000"),
    ])
    assert r.holding_basis_usd == Decimal("15000.00")
    assert r.avg_cost_per_unit_usd == Decimal("30000.00")
    # Sell everything -> nothing held -> per-unit is 0 (no div-by-zero).
    r2 = costbasis.compute([
        tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="30000"),
        tx(2, TxKind.SELL, "2025-03-01", "1.0", usd="40000"),
    ])
    assert r2.holding_sats == 0
    assert r2.avg_cost_per_unit_usd == Decimal("0.00")


def test_long_term_classification():
    txs = [
        tx(1, TxKind.BUY, "2024-01-01", "1.0", usd="40000"),
        tx(2, TxKind.SELL, "2025-06-01", "1.0", usd="60000"),  # > 365 days
    ]
    r = costbasis.compute(txs)
    assert r.disposals[0].term == "long"
    assert r.realized_long_usd == Decimal("20000.00")


def test_fifo_orders_oldest_first():
    txs = [
        tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="20000"),   # lot A
        tx(2, TxKind.BUY, "2025-02-01", "1.0", usd="30000"),   # lot B
        tx(3, TxKind.SELL, "2025-03-01", "1.5", usd="60000"),  # consumes all of A + half of B
    ]
    r = costbasis.compute(txs)
    total_basis = sum(d.basis_usd for d in r.disposals)
    assert total_basis == Decimal("35000.00")   # 20000 (A) + 15000 (half B)
    assert r.holding_sats == int(Decimal("0.5") * SATS_PER_BTC)
    assert r.holding_basis_usd == Decimal("15000.00")  # remaining half of B


def test_buy_fee_included_in_basis():
    txs = [tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="30000", fee_usd="100")]
    r = costbasis.compute(txs)
    assert r.holding_basis_usd == Decimal("30100.00")


def test_income_tracked_and_creates_lot():
    txs = [
        tx(1, TxKind.INCOME, "2025-01-01", "0.01", usd="900"),
        tx(2, TxKind.SELL, "2025-02-01", "0.01", usd="1000"),
    ]
    r = costbasis.compute(txs)
    assert r.income_usd == Decimal("900")
    assert r.disposals[0].gain_usd == Decimal("100.00")


def test_transfer_out_consumes_without_gain():
    txs = [
        tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="30000"),
        tx(2, TxKind.TRANSFER_OUT, "2025-02-01", "0.4"),
    ]
    r = costbasis.compute(txs)
    assert r.disposals == []  # no realized gain on transfer
    assert r.holding_sats == int(Decimal("0.6") * SATS_PER_BTC)
    assert r.holding_basis_usd == Decimal("18000.00")  # 60% of 30000


def test_transfer_in_without_basis_warns():
    txs = [tx(1, TxKind.TRANSFER_IN, "2025-01-01", "0.5")]
    r = costbasis.compute(txs)
    assert r.holding_sats == int(Decimal("0.5") * SATS_PER_BTC)
    assert any("no cost basis" in w for w in r.warnings)


def test_transfer_in_price_not_used_as_basis():
    # A transfer_in with only a reference price (no fiat_value) must NOT get cost basis
    # from that market price — basis carries from the original purchase.
    r = costbasis.compute([tx(1, TxKind.TRANSFER_IN, "2025-01-01", "0.5", price="90000")])
    assert r.holding_basis_usd == Decimal("0.00")
    assert any("no cost basis" in w for w in r.warnings)


def test_internal_transfer_within_account_preserves_basis():
    txs = [
        tx(1, TxKind.BUY, "2025-01-01", "0.5", usd="30000"),
        tx(2, TxKind.TRANSFER_OUT, "2025-02-01", "0.5"),   # leaves wallet A
        tx(3, TxKind.TRANSFER_IN, "2025-02-01", "0.5"),    # into wallet B (same account)
    ]
    txs[1].txid = txs[2].txid = "X"
    r = costbasis.compute(txs, internal_txids={"X"})
    # internal churn -> the original buy lot is untouched, basis preserved, no warnings
    assert r.holding_sats == int(Decimal("0.5") * SATS_PER_BTC)
    assert r.holding_basis_usd == Decimal("30000.00")
    assert r.disposals == []
    assert r.warnings == []


def test_internal_txids_detection(session):
    import datetime as dt
    from app.services import accounts as acc
    from app.services import transactions as txsvc
    a = acc.create_account(session, name="I")
    txsvc.add_transaction(session, account_id=a.id, kind=TxKind.TRANSFER_OUT,
                          timestamp=dt.datetime(2025, 1, 1), amount_sats=1000, txid="abc", external_id="abc:o")
    txsvc.add_transaction(session, account_id=a.id, kind=TxKind.TRANSFER_IN,
                          timestamp=dt.datetime(2025, 1, 1), amount_sats=1000, txid="abc", external_id="abc:i")
    txsvc.add_transaction(session, account_id=a.id, kind=TxKind.TRANSFER_IN,
                          timestamp=dt.datetime(2025, 1, 2), amount_sats=500, txid="ext", external_id="ext:i")
    assert costbasis.internal_txids(session) == {"abc"}


def test_cross_account_basis_carry(session):
    import datetime as dt
    from app.services import accounts as acc
    from app.services import transactions as txsvc
    A = acc.create_account(session, name="A")
    B = acc.create_account(session, name="B")
    half = int(Decimal("0.5") * SATS_PER_BTC)
    txsvc.add_transaction(session, account_id=A.id, kind=TxKind.BUY, timestamp=dt.datetime(2025, 1, 1),
                          amount_sats=half, fiat_value=Decimal("30000"))
    txsvc.add_transaction(session, account_id=A.id, kind=TxKind.TRANSFER_OUT, timestamp=dt.datetime(2025, 2, 1),
                          amount_sats=half, txid="X", external_id="X:out")
    txsvc.add_transaction(session, account_id=B.id, kind=TxKind.TRANSFER_IN, timestamp=dt.datetime(2025, 2, 1),
                          amount_sats=half, txid="X", external_id="X:in")

    # Before reconcile: B received coins with unknown basis.
    assert costbasis.compute_account(session, B.id).holding_basis_usd == Decimal("0.00")

    n = costbasis.reconcile_internal_transfers(session)
    assert n == 1

    # After: B's basis is the $30,000 carried from A; A is emptied.
    assert costbasis.compute_account(session, B.id).holding_basis_usd == Decimal("30000.00")
    a_after = costbasis.compute_account(session, A.id)
    assert a_after.holding_sats == 0


def test_transfer_in_fiat_value_is_not_basis():
    # A transfer_in carrying a receipt-time USD value (e.g. an exchange "receive" row) must NOT
    # establish basis at that FMV — basis comes from carryover, else 0 (audit P0).
    t = tx(1, TxKind.TRANSFER_IN, "2025-01-01", "0.5", usd="30000")
    assert costbasis.compute([t]).holding_basis_usd == Decimal("0.00")
    # With a documented carryover it uses that, still ignoring the FMV.
    t.carried_basis_usd = Decimal("12000")
    assert costbasis.compute([t]).holding_basis_usd == Decimal("12000.00")


def test_usd_value_always_available_for_display():
    # Explicit fiat_value wins (buy = its basis).
    assert tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="30000").usd_value == Decimal("30000.00")
    # Transfer with only a reference price still shows an FMV (price x amount), informational.
    t = tx(2, TxKind.TRANSFER_IN, "2025-01-01", "0.5", price="40000")
    assert t.fiat_value is None and t.usd_value == Decimal("20000.00")
    # No price yet -> nothing to show.
    assert tx(3, TxKind.TRANSFER_OUT, "2025-01-01", "0.5").usd_value is None


def test_long_term_boundary_leap_year():
    # Exactly one year across a leap year is NOT long-term (the old days>365 test wrongly said it was).
    assert not costbasis._is_long_term(dt.datetime(2024, 1, 1), dt.datetime(2025, 1, 1))
    assert costbasis._is_long_term(dt.datetime(2024, 1, 1), dt.datetime(2025, 1, 2))
    # A sale LATER IN THE DAY on the anniversary is still short-term (date comparison, not time).
    assert not costbasis._is_long_term(dt.datetime(2024, 1, 1, 9, 0), dt.datetime(2025, 1, 1, 17, 0))
    # Feb-29 acquisition: anniversary treated as Mar 1.
    assert not costbasis._is_long_term(dt.datetime(2020, 2, 29), dt.datetime(2021, 2, 28))
    assert costbasis._is_long_term(dt.datetime(2020, 2, 29), dt.datetime(2021, 3, 2))


def test_reclassify_onchain_cross_wallet_to_transfer(session):
    # A standalone send from wallet A (sell) and the matching receive in wallet B (buy) under
    # the SAME txid, same owner, must be relabeled as an internal transfer (both sides loaded).
    import datetime as dt
    from app.services import accounts as acc
    from app.services import transactions as txsvc
    A = acc.create_account(session, name="A")
    B = acc.create_account(session, name="B")
    wa = acc.add_wallet(session, A.id, "wa", "xpub", xpub=None)
    wb = acc.add_wallet(session, B.id, "wb", "xpub", xpub=None)
    half = int(Decimal("0.5") * SATS_PER_BTC)
    txsvc.add_transaction(session, account_id=A.id, wallet_id=wa.id, kind=TxKind.SELL,
                          timestamp=dt.datetime(2025, 2, 1), amount_sats=half, txid="SHARED",
                          source=f"xpub:{wa.id}", external_id="SHARED:out")
    txsvc.add_transaction(session, account_id=B.id, wallet_id=wb.id, kind=TxKind.BUY,
                          timestamp=dt.datetime(2025, 2, 1), amount_sats=half, txid="SHARED",
                          source=f"xpub:{wb.id}", external_id="SHARED:in")
    assert costbasis.reclassify_onchain_transfers(session) == 2
    kinds = {t.kind for t in session.scalars(__import__("sqlalchemy").select(Transaction))}
    assert kinds == {TxKind.TRANSFER_OUT, TxKind.TRANSFER_IN}


def test_opening_balance_creates_lot_not_income():
    r = costbasis.compute([tx(1, TxKind.OPENING, "2023-01-01", "1.0", usd="16000")])
    assert r.holding_basis_usd == Decimal("16000.00")
    assert r.income_usd == Decimal("0")   # opening balance is an acquisition, not income


def test_lot_methods_fifo_lifo_hifo():
    # Two buys at different prices, then sell 1 BTC for $50k.
    base = [
        tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="20000"),   # cheap, oldest
        tx(2, TxKind.BUY, "2025-02-01", "1.0", usd="40000"),   # expensive, newest
    ]
    sell = tx(3, TxKind.SELL, "2025-03-01", "1.0", usd="50000")
    fifo = costbasis.compute(base + [sell], method="fifo")
    lifo = costbasis.compute(base + [sell], method="lifo")
    hifo = costbasis.compute(base + [sell], method="hifo")
    assert fifo.realized_total_usd == Decimal("30000.00")   # 50k - 20k (oldest)
    assert lifo.realized_total_usd == Decimal("10000.00")   # 50k - 40k (newest)
    assert hifo.realized_total_usd == Decimal("10000.00")   # 50k - 40k (highest basis)
    # remaining lot differs: fifo keeps the $40k lot, hifo/lifo keep the $20k lot
    assert fifo.holding_basis_usd == Decimal("40000.00")
    assert hifo.holding_basis_usd == Decimal("20000.00")


def test_hifo_medium_ledger_uses_fast_selector():
    import time

    txs = []
    tid = 1
    for i in range(6000):
        txs.append(tx(tid, TxKind.BUY, f"2025-01-{(i % 28) + 1:02d}", "0.001", usd=str(20 + (i % 200))))
        tid += 1
        if i % 4 == 0:
            txs.append(tx(tid, TxKind.SELL, f"2025-02-{(i % 28) + 1:02d}", "0.00025", usd="25"))
            tid += 1

    start = time.perf_counter()
    result = costbasis.compute(txs, method="hifo")
    elapsed = time.perf_counter() - start

    assert result.disposals
    assert result.open_lots
    assert elapsed < 2.0


def test_no_shared_txid_transfer_is_not_auto_carried(session):
    # Coins through an untracked address (no shared txid) are NOT auto-reconciled — amount+date
    # matching was removed. The destination keeps no basis until the user confirms it in the
    # reconciliation inbox (by a shared intermediary address).
    import datetime as dt
    from app.services import accounts as acc
    from app.services import transactions as txsvc
    ex = acc.create_account(session, name="Coinbase")
    cold = acc.create_account(session, name="Cold")
    amt = int(Decimal("0.2") * SATS_PER_BTC)
    txsvc.add_transaction(session, account_id=ex.id, kind=TxKind.BUY, timestamp=dt.datetime(2025, 1, 1),
                          amount_sats=amt, fiat_value=Decimal("18000"))
    txsvc.add_transaction(session, account_id=ex.id, kind=TxKind.TRANSFER_OUT,
                          timestamp=dt.datetime(2025, 2, 1, 10), amount_sats=amt)
    txsvc.add_transaction(session, account_id=cold.id, kind=TxKind.TRANSFER_IN,
                          timestamp=dt.datetime(2025, 2, 1, 12), amount_sats=amt - 5000,
                          txid="deposit1", external_id="deposit1:in")
    assert costbasis.reconcile_internal_transfers(session) == 0
    assert costbasis.compute_account(session, cold.id).holding_basis_usd == Decimal("0.00")


def test_carry_disabled_ignores_carried_basis():
    t = tx(1, TxKind.TRANSFER_IN, "2025-01-01", "0.5")
    t.carried_basis_usd = Decimal("30000")
    t.carry_disabled = True
    assert costbasis.compute([t]).holding_basis_usd == Decimal("0.00")   # opted out -> fresh
    t.carry_disabled = False
    assert costbasis.compute([t]).holding_basis_usd == Decimal("30000.00")  # opted in -> carried


def test_transfer_out_records_consumed_lot_dates():
    txs = [
        tx(1, TxKind.BUY, "2024-01-01", "0.5", usd="20000"),
        tx(2, TxKind.TRANSFER_OUT, "2025-02-01", "0.5"),
    ]
    txs[1].txid = "g1"
    r = costbasis.compute(txs)
    assert r.transfer_out_basis["g1"] == Decimal("20000.00")
    lots = r.transfer_out_lots["g1"]
    assert lots and lots[0]["acquired"].year == 2024   # acquisition date captured for the gift statement


def test_transfer_to_different_owner_does_not_carry_basis(session):
    import datetime as dt
    from app.services import accounts as acc
    from app.services import transactions as txsvc
    me = acc.create_account(session, name="Mine")                 # owner "" = you
    bro = acc.create_account(session, name="Brother", owner="Bob")
    half = int(Decimal("0.5") * SATS_PER_BTC)
    txsvc.add_transaction(session, account_id=me.id, kind=TxKind.BUY, timestamp=dt.datetime(2025, 1, 1),
                          amount_sats=half, fiat_value=Decimal("30000"))
    txsvc.add_transaction(session, account_id=me.id, kind=TxKind.TRANSFER_OUT, timestamp=dt.datetime(2025, 2, 1),
                          amount_sats=half, txid="G", external_id="G:out")
    txsvc.add_transaction(session, account_id=bro.id, kind=TxKind.TRANSFER_IN, timestamp=dt.datetime(2025, 2, 1),
                          amount_sats=half, txid="G", external_id="G:in")
    # Sending to a different owner is NOT a self-transfer.
    assert "G" not in costbasis.internal_txids(session)
    assert costbasis.reconcile_internal_transfers(session) == 0
    # Brother gets a fresh (unset) basis, NOT your $30k.
    assert costbasis.compute_account(session, bro.id).holding_basis_usd == Decimal("0.00")


def test_account_detail_shows_cost_basis(client):
    import re
    client.post("/accounts", data={"name": "CBAcct"})
    aid = re.search(r"/accounts/(\d+)", client.get("/accounts").text).group(1)
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "buy", "timestamp": "2025-01-01", "amount_btc": "1.0", "fiat_value": "30000"})
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "sell", "timestamp": "2025-03-01", "amount_btc": "0.5", "fiat_value": "20000"})
    r = client.get(f"/accounts/{aid}")
    assert r.status_code == 200
    assert "Cost basis" in r.text
    assert "5,000" in r.text  # realized short-term gain $5,000


def test_sell_exceeding_lots_warns_and_zero_basis():
    txs = [
        tx(1, TxKind.BUY, "2025-01-01", "0.1", usd="9000"),
        tx(2, TxKind.SELL, "2025-02-01", "0.5", usd="50000"),
    ]
    r = costbasis.compute(txs)
    assert any("exceeds tracked lots" in w for w in r.warnings)
    # total realized = proceeds 50000 - basis 9000 (only 0.1 had basis)
    assert r.realized_total_usd == Decimal("41000.00")
