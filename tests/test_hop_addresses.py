"""Address-based fuzzy-hop detection: the scanner captures the foreign address one hop from our
coins (a spend's destination, an inflow's funder), and the reconciliation inbox matches a
known->unknown->known self-transfer by that shared intermediary address (robust to amount/time
drift), not by amount+date."""
import datetime as dt
from decimal import Decimal

from sqlalchemy import select

from app.models import SATS_PER_BTC, HopAddress, TxKind
from app.services import accounts as acc
from app.services import costbasis
from app.services import transactions as txsvc
from app.services.importers import xpub
from app.services.script import scripthash

BTC = SATS_PER_BTC
BIP84_ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868Rv"
              "UUkgDKf31mGDtKsAYz2oz2AGutZYs")


class MockElectrum:
    def __init__(self, histories, txs):
        self._h, self._t = histories, txs

    def get_history(self, sh):
        return self._h.get(sh, [])

    def get_transaction(self, txid, verbose=True):
        return self._t[txid]


def _hop_mock():
    """txA: a FOREIGN funder ('funder-addr', via prevtx txF) pays our r0 (inflow -> funder
    captured). txC: spends r0 to a FOREIGN dest ('exit-addr') + our change c0 (outflow ->
    destination captured; change excluded)."""
    r0 = xpub.derive_addresses(BIP84_ZPUB, change=0, count=1, start=0)[0][1]
    c0 = xpub.derive_addresses(BIP84_ZPUB, change=1, count=1, start=0)[0][1]
    histories = {
        scripthash(r0): [{"tx_hash": "txA", "height": 800000}, {"tx_hash": "txC", "height": 800002}],
        scripthash(c0): [{"tx_hash": "txC", "height": 800002}],
    }
    txs = {
        # Funder's prior tx — not in our history; fetched only to resolve the input address.
        "txF": {"vin": [], "vout": [{"value": 0.02, "scriptPubKey": {"address": "funder-addr"}}],
                "blocktime": 1699990000},
        "txA": {"vin": [{"txid": "txF", "vout": 0}],
                "vout": [{"value": 0.01, "scriptPubKey": {"address": r0}}], "blocktime": 1700000000},
        "txC": {"vin": [{"txid": "txA", "vout": 0}],
                "vout": [{"value": 0.005, "scriptPubKey": {"address": "exit-addr"}},
                         {"value": 0.0049, "scriptPubKey": {"address": c0}}],
                "blocktime": 1700002000},
    }
    return MockElectrum(histories, txs), (r0, c0)


def test_scanner_captures_destination_and_funder():
    client, (r0, c0) = _hop_mock()
    scan = xpub.scan_xpub(client, BIP84_ZPUB, gap_limit=2)
    assert scan.error == ""
    eps = {(e.txid, e.direction, e.address) for e in scan.endpoints}
    assert ("txC", "out", "exit-addr") in eps    # spend destination captured (free, from vout)
    assert ("txA", "in", "funder-addr") in eps   # inflow funder captured (via prevtx fetch)
    # Our own change address and our own input are NOT recorded as foreign endpoints.
    assert not any(addr in (r0, c0) for _, _, addr in eps)


def test_persist_endpoints_idempotent(session):
    a = acc.create_account(session, name="Cold")
    w = acc.add_wallet(session, account_id=a.id, label="zpub", wtype="xpub",
                       xpub=BIP84_ZPUB, gap_limit=2)
    client, _ = _hop_mock()
    xpub.import_xpub(session, wallet=w, client=client)
    rows = list(session.scalars(select(HopAddress).where(HopAddress.wallet_id == w.id)))
    assert {(r.direction, r.address) for r in rows} == {("out", "exit-addr"), ("in", "funder-addr")}
    xpub.import_xpub(session, wallet=w, client=client)  # re-sync
    assert len(list(session.scalars(select(HopAddress).where(HopAddress.wallet_id == w.id)))) == 2


def _add_hop(session, account_id, wallet_id, txid, direction, address):
    session.add(HopAddress(account_id=account_id, wallet_id=wallet_id, txid=txid,
                           direction=direction, address=address, created_at=dt.datetime(2025, 1, 1)))
    session.commit()


def test_address_match_beats_amount_and_time_drift(session):
    """A real hop can change sats a lot and span weeks — amount+date would miss it, but a shared
    intermediary address matches."""
    A = acc.create_account(session, name="A")
    B = acc.create_account(session, name="B")
    wa = acc.add_wallet(session, A.id, "wa", "xpub", xpub=None)
    wb = acc.add_wallet(session, B.id, "wb", "xpub", xpub=None)
    o = txsvc.add_transaction(session, account_id=A.id, wallet_id=wa.id, kind=TxKind.SELL,
                              timestamp=dt.datetime(2024, 1, 1), amount_sats=int(0.5 * BTC),
                              txid="tOut", external_id="tOut:out", source=f"xpub:{wa.id}")
    i = txsvc.add_transaction(session, account_id=B.id, wallet_id=wb.id, kind=TxKind.BUY,
                              timestamp=dt.datetime(2024, 3, 1), amount_sats=int(0.3 * BTC),
                              txid="tIn", external_id="tIn:in", source=f"xpub:{wb.id}")
    _add_hop(session, A.id, wa.id, "tOut", "out", "U-INTERMEDIARY")
    _add_hop(session, B.id, wb.id, "tIn", "in", "U-INTERMEDIARY")

    sugg = costbasis.suggest_transfers(session)
    assert len(sugg) == 1
    s = sugg[0]
    assert s.out_tx.id == o.id and s.in_tx.id == i.id
    assert s.confidence == "high"            # not downgraded by the 0.2 BTC / 60-day gap
    assert s.shared_address == "U-INTERMEDIARY"


def test_no_shared_address_yields_no_suggestion(session):
    # Without a shared intermediary address there is no suggestion — amount+date matching was
    # removed, so a close-amount/close-date pair is NOT proposed.
    A = acc.create_account(session, name="A2")
    B = acc.create_account(session, name="B2")
    txsvc.add_transaction(session, account_id=A.id, kind=TxKind.TRANSFER_OUT,
                          timestamp=dt.datetime(2024, 2, 1, 10), amount_sats=int(0.2 * BTC),
                          external_id="o2")
    txsvc.add_transaction(session, account_id=B.id, kind=TxKind.TRANSFER_IN,
                          timestamp=dt.datetime(2024, 2, 1, 12), amount_sats=int(0.2 * BTC) - 3000,
                          txid="dep2", external_id="dep2:in")
    assert costbasis.suggest_transfers(session) == []


def test_address_match_excludes_shared_txid_and_reviewed(session):
    A = acc.create_account(session, name="A3")
    B = acc.create_account(session, name="B3")
    wa = acc.add_wallet(session, A.id, "wa", "xpub", xpub=None)
    wb = acc.add_wallet(session, B.id, "wb", "xpub", xpub=None)
    # Same txid on both sides = a proven shared-txid transfer -> handled by the auto reconciler,
    # never an inbox suggestion, even though they also share an intermediary endpoint.
    txsvc.add_transaction(session, account_id=A.id, wallet_id=wa.id, kind=TxKind.SELL,
                          timestamp=dt.datetime(2024, 1, 1), amount_sats=int(0.5 * BTC),
                          txid="SAME", external_id="SAME:out")
    txsvc.add_transaction(session, account_id=B.id, wallet_id=wb.id, kind=TxKind.BUY,
                          timestamp=dt.datetime(2024, 1, 1), amount_sats=int(0.5 * BTC),
                          txid="SAME", external_id="SAME:in")
    _add_hop(session, A.id, wa.id, "SAME", "out", "U")
    _add_hop(session, B.id, wb.id, "SAME", "in", "U")
    assert costbasis.suggest_transfers(session) == []


def test_confirm_address_match_carries_coarsely(session):
    A = acc.create_account(session, name="Ac")
    B = acc.create_account(session, name="Bc")
    wa = acc.add_wallet(session, A.id, "wa", "xpub", xpub=None)
    wb = acc.add_wallet(session, B.id, "wb", "xpub", xpub=None)
    txsvc.add_transaction(session, account_id=A.id, wallet_id=wa.id, kind=TxKind.BUY,
                          timestamp=dt.datetime(2024, 1, 1), amount_sats=int(0.5 * BTC),
                          fiat_value=Decimal("20000"), external_id="buyA")
    o = txsvc.add_transaction(session, account_id=A.id, wallet_id=wa.id, kind=TxKind.SELL,
                              timestamp=dt.datetime(2024, 2, 1), amount_sats=int(0.5 * BTC),
                              txid="tOut", external_id="tOut:out", source=f"xpub:{wa.id}")
    i = txsvc.add_transaction(session, account_id=B.id, wallet_id=wb.id, kind=TxKind.BUY,
                              timestamp=dt.datetime(2024, 5, 1), amount_sats=int(0.49 * BTC),
                              txid="tIn", external_id="tIn:in", source=f"xpub:{wb.id}")
    _add_hop(session, A.id, wa.id, "tOut", "out", "U")
    _add_hop(session, B.id, wb.id, "tIn", "in", "U")

    ok, _ = costbasis.confirm_transfer(session, o.id, i.id)
    assert ok
    session.refresh(o); session.refresh(i)
    assert o.kind == TxKind.TRANSFER_OUT and i.kind == TxKind.TRANSFER_IN
    assert i.carried_basis_usd == Decimal("20000.00")   # basis carried...
    assert i.carried_lots is None                        # ...coarsely (no fragment rebuild for a fuzzy hop)


def test_reconcile_route_renders_shared_address(client):
    """The inbox page renders an address-matched suggestion (guards the template wiring)."""
    from app.db import SessionLocal
    with SessionLocal() as s:
        A = acc.create_account(s, name="RouteA")
        B = acc.create_account(s, name="RouteB")
        wa = acc.add_wallet(s, A.id, "wa", "xpub", xpub=None)
        wb = acc.add_wallet(s, B.id, "wb", "xpub", xpub=None)
        txsvc.add_transaction(s, account_id=A.id, wallet_id=wa.id, kind=TxKind.SELL,
                              timestamp=dt.datetime(2024, 1, 1), amount_sats=int(0.5 * BTC),
                              txid="rOut", external_id="rOut:out", source=f"xpub:{wa.id}")
        txsvc.add_transaction(s, account_id=B.id, wallet_id=wb.id, kind=TxKind.BUY,
                              timestamp=dt.datetime(2024, 4, 1), amount_sats=int(0.31 * BTC),
                              txid="rIn", external_id="rIn:in", source=f"xpub:{wb.id}")
        _add_hop(s, A.id, wa.id, "rOut", "out", "bc1q-shared-intermediary")
        _add_hop(s, B.id, wb.id, "rIn", "in", "bc1q-shared-intermediary")
    r = client.get("/reconcile")
    assert r.status_code == 200
    assert "shared address" in r.text and "bc1q-shared-intermediary" in r.text


def test_migration_0004_creates_hop_addresses(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    url = f"sqlite:///{(tmp_path / 'm.sqlite').as_posix()}"
    monkeypatch.setattr("app.config.DATABASE_URL", url)
    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    assert "hop_addresses" in inspect(eng).get_table_names()
    eng.dispose()
