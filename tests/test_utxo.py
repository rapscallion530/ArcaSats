"""Phase 1+2: UTXO inventory from the xpub scanner + privacy lints."""
import datetime as dt

from sqlalchemy import select

from app.models import Utxo
from app.services import accounts as acc
from app.services import coins as coins_svc
from app.services.importers import xpub
from app.services.script import scripthash

# BIP84 official test-vector account zpub (mnemonic "abandon ... about").
BIP84_ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868Rv"
              "UUkgDKf31mGDtKsAYz2oz2AGutZYs")


class MockElectrum:
    def __init__(self, histories, txs):
        self._h, self._t = histories, txs

    def get_history(self, sh):
        return self._h.get(sh, [])

    def get_transaction(self, txid, verbose=True):
        return self._t[txid]


def _utxo_mock():
    """txA -> recv0 (0.01); txB -> recv1 (0.02); txC spends recv0 -> external + change0 (0.0049)."""
    r = xpub.derive_addresses(BIP84_ZPUB, change=0, count=2, start=0)
    r0, r1 = r[0][1], r[1][1]
    c0 = xpub.derive_addresses(BIP84_ZPUB, change=1, count=1, start=0)[0][1]
    histories = {
        scripthash(r0): [{"tx_hash": "txA", "height": 800000}, {"tx_hash": "txC", "height": 800002}],
        scripthash(r1): [{"tx_hash": "txB", "height": 800001}],
        scripthash(c0): [{"tx_hash": "txC", "height": 800002}],
    }
    txs = {
        "txA": {"vin": [], "vout": [{"value": 0.01, "scriptPubKey": {"address": r0}}],
                "blocktime": 1700000000},
        "txB": {"vin": [], "vout": [{"value": 0.02, "scriptPubKey": {"address": r1}}],
                "blocktime": 1700001000},
        "txC": {"vin": [{"txid": "txA", "vout": 0}],
                "vout": [{"value": 0.005, "scriptPubKey": {"address": "external-dst"}},
                         {"value": 0.0049, "scriptPubKey": {"address": c0}}],
                "blocktime": 1700002000},
    }
    return MockElectrum(histories, txs), (r0, r1, c0)


def test_scanner_emits_utxos_with_spend_and_change():
    client, (r0, r1, c0) = _utxo_mock()
    scan = xpub.scan_xpub(client, BIP84_ZPUB, gap_limit=2)
    assert scan.error == ""
    by_outpoint = {(u.txid, u.vout): u for u in scan.utxos}
    assert set(by_outpoint) == {("txA", 0), ("txB", 0), ("txC", 1)}
    # recv0 was spent by txC
    assert by_outpoint[("txA", 0)].spent_txid == "txC"
    assert by_outpoint[("txA", 0)].value_sats == 1_000_000
    # recv1 still unspent
    assert by_outpoint[("txB", 0)].spent_txid is None
    # txC vout1 is our change (chain 1), unspent
    chg = by_outpoint[("txC", 1)]
    assert chg.chain == 1 and chg.spent_txid is None and chg.value_sats == 490_000
    assert chg.address == c0


def test_import_persists_utxos_and_is_idempotent(session):
    a = acc.create_account(session, name="Cold", label_kind="non-KYC")
    w = acc.add_wallet(session, account_id=a.id, label="zpub", wtype="xpub",
                       xpub=BIP84_ZPUB, gap_limit=2)
    client, _ = _utxo_mock()
    xpub.import_xpub(session, wallet=w, client=client)
    rows = list(session.scalars(select(Utxo).where(Utxo.wallet_id == w.id)))
    assert len(rows) == 3
    assert {r.is_change for r in rows} == {False, True}
    # provenance label snapshot taken from the account
    assert all(r.label_kind == "non-KYC" for r in rows)
    spent = next(r for r in rows if r.txid == "txA")
    assert spent.spent_txid == "txC"

    # Re-sync: no duplicates, spent status preserved.
    xpub.import_xpub(session, wallet=w, client=client)
    assert len(list(session.scalars(select(Utxo).where(Utxo.wallet_id == w.id)))) == 3


def test_list_utxos_unspent_and_total(session):
    a = acc.create_account(session, name="Cold2")
    w = acc.add_wallet(session, account_id=a.id, label="zpub", wtype="xpub",
                       xpub=BIP84_ZPUB, gap_limit=2)
    client, _ = _utxo_mock()
    xpub.import_xpub(session, wallet=w, client=client)
    live = coins_svc.list_utxos(session, a.id, unspent_only=True)
    assert len(live) == 2                                   # recv1 + change0
    assert coins_svc.unspent_total_sats(live) == 2_490_000  # 0.02 + 0.0049


def _add_utxo(session, account_id, wallet_id, **kw):
    defaults = dict(txid="t" + str(kw.get("vout", 0)), vout=0, value_sats=100000, address="addr",
                    created_at=dt.datetime(2025, 1, 1))
    defaults.update(kw)
    u = Utxo(account_id=account_id, wallet_id=wallet_id, **defaults)
    session.add(u)
    session.commit()
    return u


def test_privacy_warning_address_reuse(session):
    a = acc.create_account(session, name="Reuse")
    w = acc.add_wallet(session, account_id=a.id, label="w", wtype="xpub", xpub=BIP84_ZPUB)
    _add_utxo(session, a.id, w.id, txid="x1", vout=0, address="bc1qreused")
    _add_utxo(session, a.id, w.id, txid="x2", vout=0, address="bc1qreused")  # same addr again
    titles = [warn.title for warn in coins_svc.privacy_warnings(session, a.id)]
    assert "Address reuse" in titles


def test_privacy_warning_kyc_merge_across_accounts(session):
    # A spend (txid "spendX") co-spent a KYC coin and a non-KYC coin -> linkage warning.
    kyc = acc.create_account(session, name="KYC acct", label_kind="KYC")
    wk = acc.add_wallet(session, account_id=kyc.id, label="wk", wtype="xpub", xpub=BIP84_ZPUB)
    nokyc = acc.create_account(session, name="non-KYC acct", label_kind="non-KYC")
    wn = acc.add_wallet(session, account_id=nokyc.id, label="wn", wtype="xpub", xpub=BIP84_ZPUB)
    _add_utxo(session, kyc.id, wk.id, txid="a", vout=0, address="addr-k",
              label_kind="KYC", spent_txid="spendX")
    _add_utxo(session, nokyc.id, wn.id, txid="b", vout=0, address="addr-n",
              label_kind="non-KYC", spent_txid="spendX")
    warns = coins_svc.privacy_warnings(session, kyc.id)
    merge = [w for w in warns if "merged" in w.title]
    assert merge and "spendX" in merge[0].txids


def test_coins_route_renders(client):
    client.post("/accounts", data={"name": "CoinsRoute"})
    import re
    aid = re.search(r"/accounts/(\d+)", client.get("/accounts").text).group(1)
    r = client.get(f"/accounts/{aid}/coins")
    assert r.status_code == 200
    assert "Coins" in r.text and "No coins tracked yet" in r.text


def test_coins_route_renders_populated(client):
    import re
    from app.db import SessionLocal
    client.post("/accounts", data={"name": "PopCoins"})
    aid = int(re.search(r"/accounts/(\d+)", client.get("/accounts").text).group(1))
    with SessionLocal() as s:
        s.add(Utxo(account_id=aid, wallet_id=1, txid="aabbccddeeff00112233", vout=1,
                   value_sats=1_234_567, address="bc1qexampleaddressxxxxxxxxxxx0000",
                   is_change=True, label_kind="KYC", created_at=dt.datetime(2025, 6, 1)))
        s.commit()
    r = client.get(f"/accounts/{aid}/coins")
    assert r.status_code == 200
    # populated table renders: address prefix, change badge, outpoint
    assert "bc1qexamplea" in r.text and "change" in r.text and "aabbccddee" in r.text
