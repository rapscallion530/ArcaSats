"""KYC/UTXO lot engine (audit #8): Layer A (KYC on the lot, fragment-rebuild carry) +
start of Layer B (KYC-aware disposal selection)."""
import datetime as dt
from decimal import Decimal

from app.models import SATS_PER_BTC, Transaction, TxKind
from app.services import accounts as acc
from app.services import costbasis
from app.services import taxforms
from app.services import transactions as txsvc

BTC = SATS_PER_BTC


def _tx(i, kind, date, btc, usd=None, kyc=""):
    """A detached Transaction for unit-level compute() tests (mirrors test_costbasis.tx)."""
    t = Transaction(
        id=i, account_id=1, kind=kind, timestamp=dt.datetime.fromisoformat(date),
        amount_sats=int(Decimal(str(btc)) * BTC),
        fiat_value=Decimal(str(usd)) if usd is not None else None,
    )
    t.kyc_origin = kyc
    return t


# --- Layer A: snapshot at import --------------------------------------------
def test_kyc_origin_snapshot_at_import(session):
    a = acc.create_account(session, name="CB", label_kind="KYC")
    buy = txsvc.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                                timestamp=dt.datetime(2025, 1, 1), amount_sats=BTC,
                                fiat_value=Decimal("30000"))
    inc = txsvc.add_transaction(session, account_id=a.id, kind=TxKind.INCOME,
                                timestamp=dt.datetime(2025, 1, 2), amount_sats=BTC // 100,
                                fiat_value=Decimal("900"))
    # A transfer_in's provenance is the SOURCE's (carried in), so it is NOT snapshotted here.
    tin = txsvc.add_transaction(session, account_id=a.id, kind=TxKind.TRANSFER_IN,
                                timestamp=dt.datetime(2025, 1, 3), amount_sats=BTC // 2,
                                txid="z", external_id="z:in")
    assert buy.kyc_origin == "KYC"
    assert inc.kyc_origin == "KYC"
    assert tin.kyc_origin == ""


def test_relabel_resnapshots_direct_acquisitions(session):
    a = acc.create_account(session, name="R", label_kind="non-KYC")
    buy = txsvc.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                                timestamp=dt.datetime(2025, 1, 1), amount_sats=BTC,
                                fiat_value=Decimal("30000"))
    assert buy.kyc_origin == "non-KYC"
    acc.update_account(session, a.id, name="R", label_kind="KYC")
    session.refresh(buy)
    assert buy.kyc_origin == "KYC"   # relabel propagates to existing direct acquisitions


# --- Layer A: reporting ------------------------------------------------------
def test_holding_and_realized_by_kyc():
    txs = [
        _tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="20000", kyc="KYC"),
        _tx(2, TxKind.BUY, "2025-01-02", "1.0", usd="30000", kyc="non-KYC"),
        _tx(3, TxKind.SELL, "2025-03-01", "0.5", usd="15000"),  # FIFO consumes the KYC lot first
    ]
    r = costbasis.compute(txs)
    hold = r.holding_by_kyc
    # 0.5 KYC left + 1.0 non-KYC left
    assert hold["KYC"]["sats"] == BTC // 2
    assert hold["non-KYC"]["sats"] == BTC
    assert hold["KYC"]["basis_usd"] == Decimal("10000.00")
    assert hold["non-KYC"]["basis_usd"] == Decimal("30000.00")
    # the realized disposal consumed the KYC lot
    realized = r.realized_by_kyc
    assert realized["KYC"]["total"] == Decimal("5000.00")   # 15000 - 10000
    assert "non-KYC" not in realized


# --- Layer A: fragment-rebuild carry ----------------------------------------
def test_fragment_rebuild_tacks_holding_period_and_kyc(session):
    """A self-transfer must NOT reset the holding-period clock: a coin bought >1yr before the
    sale stays long-term across the transfer, and its KYC label carries."""
    A = acc.create_account(session, name="A", label_kind="KYC")
    B = acc.create_account(session, name="B", label_kind="non-KYC")  # dest label must NOT win
    txsvc.add_transaction(session, account_id=A.id, kind=TxKind.BUY,
                          timestamp=dt.datetime(2024, 1, 1), amount_sats=BTC,
                          fiat_value=Decimal("40000"))
    txsvc.add_transaction(session, account_id=A.id, kind=TxKind.TRANSFER_OUT,
                          timestamp=dt.datetime(2024, 6, 1), amount_sats=BTC,
                          txid="X", external_id="X:out")
    txsvc.add_transaction(session, account_id=B.id, kind=TxKind.TRANSFER_IN,
                          timestamp=dt.datetime(2024, 6, 1), amount_sats=BTC,
                          txid="X", external_id="X:in")
    txsvc.add_transaction(session, account_id=B.id, kind=TxKind.SELL,
                          timestamp=dt.datetime(2025, 3, 1), amount_sats=BTC,
                          fiat_value=Decimal("60000"))
    assert costbasis.reconcile_internal_transfers(session) == 1
    rb = costbasis.compute_account(session, B.id)
    assert len(rb.disposals) == 1
    d = rb.disposals[0]
    # Bought 2024-01-01, sold 2025-03-01 -> LONG term (would be SHORT if the transfer reset it).
    assert d.term == "long"
    assert d.basis_usd == Decimal("40000.00")   # original basis carried
    assert d.kyc_origin == "KYC"                 # source label, not B's "non-KYC"


def test_fragment_rebuild_many_hops_preserve_origin(session):
    """Many exact shared-txid hops (A→B→C→D), each between two of the user's own wallets, carry
    cost basis the whole way: the original acquisition date and KYC label survive every hop."""
    accts = [acc.create_account(session, name=n, label_kind=("KYC" if n == "A" else ""))
             for n in ("A", "B", "C", "D")]
    txsvc.add_transaction(session, account_id=accts[0].id, kind=TxKind.BUY,
                          timestamp=dt.datetime(2023, 5, 1), amount_sats=BTC,
                          fiat_value=Decimal("25000"))
    # Three hops: A→B (H1), B→C (H2), C→D (H3) — each a shared-txid self-transfer.
    for hop, (src, dst) in enumerate(zip(accts, accts[1:]), start=1):
        txid = f"H{hop}"
        ts = dt.datetime(2023, 5 + hop, 1)
        txsvc.add_transaction(session, account_id=src.id, kind=TxKind.TRANSFER_OUT, timestamp=ts,
                              amount_sats=BTC, txid=txid, external_id=f"{txid}:out")
        txsvc.add_transaction(session, account_id=dst.id, kind=TxKind.TRANSFER_IN, timestamp=ts,
                              amount_sats=BTC, txid=txid, external_id=f"{txid}:in")
    costbasis.reconcile_internal_transfers(session)
    # A, B, C are emptied; D holds the coin with the ORIGINAL basis/date/KYC carried through 3 hops.
    for empty in accts[:3]:
        assert costbasis.compute_account(session, empty.id).holding_sats == 0
    rd = costbasis.compute_account(session, accts[3].id)
    assert rd.holding_basis_usd == Decimal("25000.00")
    assert rd.open_lots and rd.open_lots[0].kyc_origin == "KYC"
    assert rd.open_lots[0].acquired.year == 2023 and rd.open_lots[0].acquired.month == 5


def test_merge_kyc_conservative():
    # The conservative collapse used where coins MUST carry one label (deferred UTXO
    # consolidation): mixed inputs -> "KYC"; a single label -> itself; none -> "".
    assert costbasis._merge_kyc(["KYC", "non-KYC"]) == "KYC"
    assert costbasis._merge_kyc(["non-KYC", "", "non-KYC"]) == "non-KYC"
    assert costbasis._merge_kyc(["", ""]) == ""


def test_fuzzy_hop_does_not_auto_carry(session):
    """A no-shared-txid hop is never auto-carried (amount+date matching was removed): the default
    reconcile leaves it a clean break, and the destination gets no basis until the user confirms
    it in the reconciliation inbox (by a shared intermediary address)."""
    ex = acc.create_account(session, name="Ex", label_kind="KYC")
    cold = acc.create_account(session, name="Cold")
    out_amt = int(Decimal("0.2") * BTC)
    txsvc.add_transaction(session, account_id=ex.id, kind=TxKind.BUY,
                          timestamp=dt.datetime(2024, 1, 1), amount_sats=out_amt,
                          fiat_value=Decimal("18000"))
    txsvc.add_transaction(session, account_id=ex.id, kind=TxKind.TRANSFER_OUT,
                          timestamp=dt.datetime(2024, 6, 1, 10), amount_sats=out_amt)
    txsvc.add_transaction(session, account_id=cold.id, kind=TxKind.TRANSFER_IN,
                          timestamp=dt.datetime(2024, 6, 1, 12), amount_sats=out_amt - 5000,
                          txid="dep", external_id="dep:in")
    assert costbasis.reconcile_internal_transfers(session) == 0   # shared-txid only -> no carry
    assert costbasis.compute_account(session, cold.id).holding_basis_usd == Decimal("0.00")


def test_carry_disabled_skips_fragments():
    t = _tx(1, TxKind.TRANSFER_IN, "2025-02-01", "1.0")
    t.carried_lots = '[{"acquired": "2024-01-01T00:00:00", "sats": 100000000, "basis": "40000", "kyc": "KYC"}]'
    t.carry_disabled = True
    assert costbasis.compute([t]).holding_basis_usd == Decimal("0.00")  # opted out -> no carry
    t.carry_disabled = False
    r = costbasis.compute([t])
    assert r.holding_basis_usd == Decimal("40000.00")
    assert r.open_lots[0].acquired.year == 2024 and r.open_lots[0].kyc_origin == "KYC"


# --- Layer B: KYC-aware disposal selection ----------------------------------
def _two_class_lots(sell_btc="1.0"):
    return [
        _tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="20000", kyc="KYC"),       # oldest, cheapest
        _tx(2, TxKind.BUY, "2025-02-01", "1.0", usd="40000", kyc="non-KYC"),   # newest, dearest
        _tx(3, TxKind.SELL, "2025-03-01", sell_btc, usd="50000"),
    ]


def test_disposal_priority_non_kyc_first():
    r = costbasis.compute(_two_class_lots(), method="fifo", priority="non_kyc_first")
    assert r.disposals[0].kyc_origin == "non-KYC"        # spent the non-KYC lot despite FIFO age
    assert r.realized_by_kyc["non-KYC"]["total"] == Decimal("10000.00")  # 50k - 40k
    assert r.holding_by_kyc["KYC"]["sats"] == BTC          # KYC coins preserved


def test_disposal_priority_kyc_first():
    r = costbasis.compute(_two_class_lots(), method="lifo", priority="kyc_first")
    assert r.disposals[0].kyc_origin == "KYC"             # KYC class first, despite LIFO
    assert r.holding_by_kyc["non-KYC"]["sats"] == BTC


def test_priority_falls_back_to_method_when_class_exhausted():
    # Sell 1.5 BTC, non-KYC first: consumes all 1.0 non-KYC, then 0.5 of KYC.
    r = costbasis.compute(_two_class_lots(sell_btc="1.5"), method="fifo", priority="non_kyc_first")
    kinds = [d.kyc_origin for d in r.disposals]
    assert kinds[0] == "non-KYC" and "KYC" in kinds
    assert r.holding_by_kyc["KYC"]["sats"] == BTC // 2


def test_priority_none_byte_identical_to_plain():
    base = _two_class_lots()
    for method in ("fifo", "lifo", "hifo"):
        a = costbasis.compute(base, method=method)
        b = costbasis.compute(base, method=method, priority="none")
        assert [(d.basis_usd, d.proceeds_usd, d.term) for d in a.disposals] == \
               [(d.basis_usd, d.proceeds_usd, d.term) for d in b.disposals]
        assert a.holding_basis_usd == b.holding_basis_usd


# --- taxforms KYC split ------------------------------------------------------
def test_taxforms_totals_by_kyc():
    r = costbasis.compute([
        _tx(1, TxKind.BUY, "2025-01-01", "1.0", usd="20000", kyc="KYC"),
        _tx(2, TxKind.SELL, "2025-03-01", "1.0", usd="30000"),
    ])
    rows = taxforms.build_rows(r, 2025)
    by_kyc = taxforms.totals_by_kyc(rows)
    assert by_kyc["KYC"]["short"] == Decimal("10000.00")
    assert by_kyc["KYC"]["total"] == Decimal("10000.00")


# --- migration 0003 backfill -------------------------------------------------
def test_migration_0003_backfills_kyc_origin(tmp_path, monkeypatch):
    """Upgrade to 0002 (pre-0003 schema), insert old rows, then upgrade to head: the migration
    adds kyc_origin and snapshots the account label onto existing direct acquisitions."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    url = f"sqlite:///{(tmp_path / 'm.sqlite').as_posix()}"
    monkeypatch.setattr("app.config.DATABASE_URL", url)  # env.py reads this on each run
    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))

    command.upgrade(cfg, "0002_drop_multiuser")  # schema WITHOUT kyc_origin/carried_lots
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO accounts (id,name,label_kind,owner,lot_method,note,created_at)"
                       " VALUES (1,'A','KYC','','fifo','','2025-01-01 00:00:00')"))
        for tid, kind in ((1, "buy"), (2, "transfer_in")):
            c.execute(text(
                "INSERT INTO transactions (id,account_id,timestamp,kind,amount_sats,fee_sats,"
                "carry_disabled,transfer_reviewed,counterparty,source,note,created_at)"
                f" VALUES ({tid},1,'2025-01-01 00:00:00','{kind}',100000,0,0,0,'','manual','',"
                "'2025-01-01 00:00:00')"))

    command.upgrade(cfg, "head")  # runs 0003: add columns + backfill
    with eng.connect() as c:
        rows = dict(c.execute(text("SELECT kind, kyc_origin FROM transactions")).all())
        prio = c.execute(text("SELECT disposal_priority FROM accounts WHERE id=1")).scalar()
    eng.dispose()
    assert rows["buy"] == "KYC"          # direct acquisition got the account label
    assert rows["transfer_in"] == ""     # transfer_in left blank (source's label carries instead)
    assert prio == "none"                # new account column defaulted


# --- route-level wiring ------------------------------------------------------
def _aid_by_name(client, name: str) -> str:
    """The account id for `name` (the client fixture shares one file DB, so 'first match' is
    unreliable once other tests have created accounts)."""
    import re
    grid = client.get("/accounts").text
    # Tempered dot — don't let .*? cross into the NEXT card's /accounts/ href.
    m = re.search(r'/accounts/(\d+)"(?:(?!/accounts/).)*?<h3[^>]*>\s*' + re.escape(name),
                  grid, re.DOTALL)
    assert m, f"account {name!r} not found in grid"
    return m.group(1)


def test_cost_basis_tile_shows_kyc_breakdown(client):
    client.post("/accounts", data={"name": "KYCTileAcct", "label_kind": "KYC"})
    aid = _aid_by_name(client, "KYCTileAcct")
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "buy", "timestamp": "2025-01-01", "amount_btc": "1.0", "fiat_value": "30000"})
    r = client.get(f"/accounts/{aid}")
    assert r.status_code == 200
    assert "Balance by KYC origin" in r.text
    assert "KYC" in r.text


def test_disposal_priority_round_trips_and_applies(client):
    client.post("/accounts", data={"name": "PrioAcct", "label_kind": "KYC",
                                    "disposal_priority": "non_kyc_first"})
    aid = _aid_by_name(client, "PrioAcct")
    # The edit form should reflect the saved priority.
    assert 'value="non_kyc_first" selected' in client.get(f"/accounts/{aid}/edit-form").text

