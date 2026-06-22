"""Phase 3: xpub derivation, address->scripthash, and scan via a mock Electrum."""
from app.models import TxKind
from app.services import accounts as acc
from app.services import transactions as tx_svc
from app.services.importers import xpub
from app.services.script import address_to_scriptpubkey, scripthash

GENESIS_ADDR = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"

# BIP84 official test-vector account zpub (mnemonic "abandon ... about").
BIP84_ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868Rv"
              "UUkgDKf31mGDtKsAYz2oz2AGutZYs")
BIP84_RECV0 = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
BIP84_RECV1 = "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"
BIP84_CHANGE0 = "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"


def test_segwit_scriptpubkey_bip173_vector():
    # BIP173 reference: bc1qw508... -> 0014<20-byte program>
    spk = address_to_scriptpubkey("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
    assert spk.hex() == "0014751e76e8199196d454941c45d1b3a323f1433bd6"


def test_p2pkh_scriptpubkey_genesis_vector():
    # Genesis address -> p2pkh scriptPubKey 76a914<hash160>88ac.
    spk = address_to_scriptpubkey(GENESIS_ADDR)
    assert spk.hex() == "76a91462e907b15cbf27d5425399ebf6f0fb50ebb88f1888ac"
    # scripthash is a deterministic 32-byte (64-hex) digest.
    sh = scripthash(GENESIS_ADDR)
    assert len(sh) == 64 and int(sh, 16) >= 0


def test_derive_bip84_vectors():
    recv = derive = xpub.derive_addresses(BIP84_ZPUB, change=0, count=2, start=0)
    assert recv[0][1] == BIP84_RECV0
    assert recv[1][1] == BIP84_RECV1
    change0 = xpub.derive_addresses(BIP84_ZPUB, change=1, count=1, start=0)
    assert change0[0][1] == BIP84_CHANGE0


class MockElectrum:
    def __init__(self, histories, txs):
        self._h = histories
        self._t = txs

    def get_history(self, sh):
        return self._h.get(sh, [])

    def get_transaction(self, txid, verbose=True):
        return self._t[txid]


def _build_mock():
    addr0 = xpub.derive_addresses(BIP84_ZPUB, change=0, count=1, start=0)[0][1]
    sh0 = scripthash(addr0)
    histories = {sh0: [{"tx_hash": "tx1", "height": 800000}, {"tx_hash": "tx2", "height": 800001}]}
    txs = {
        # external funding tx -> addr0 receives 0.001
        "tx0": {"vin": [], "vout": [{"value": 0.5, "scriptPubKey": {"address": "external-src"}}]},
        "tx1": {"vin": [{"txid": "tx0", "vout": 0}],
                "vout": [{"value": 0.001, "scriptPubKey": {"address": addr0}}],
                "blocktime": 1700000000},
        # spend addr0 entirely to an external address (transfer out)
        "tx2": {"vin": [{"txid": "tx1", "vout": 0}],
                "vout": [{"value": 0.0009, "scriptPubKey": {"address": "external-dst"}}],
                "blocktime": 1700100000},
    }
    return MockElectrum(histories, txs)


def test_detect_script_type_probes_beyond_index_zero():
    # The zpub's default is p2wpkh, but here ONLY a p2sh-p2wpkh address at index 5 is used
    # (index 0 is an unused gap). Old detection (index-0 only) would mis-fall-back to the prefix
    # default; the fix must probe deeper and return p2sh-p2wpkh.
    used = xpub.derive_addresses(BIP84_ZPUB, change=0, count=1, start=5, script_type="p2sh-p2wpkh")[0][1]
    mock = MockElectrum({scripthash(used): [{"tx_hash": "tx9", "height": 800000}]}, {})
    assert xpub.detect_script_type(mock, BIP84_ZPUB) == "p2sh-p2wpkh"


def test_scan_xpub_net_amounts():
    client = _build_mock()
    res = xpub.scan_xpub(client, BIP84_ZPUB, gap_limit=2)
    assert res.error == ""
    by_txid = {t.txid: t for t in res.txs}
    assert by_txid["tx1"].net_sats == 100_000     # +0.001 received
    assert by_txid["tx2"].net_sats == -100_000    # -0.001 spent


def test_scan_descriptor_finds_multisig_tx():
    # A 2-of-2 wsh(sortedmulti) descriptor; the mock node has history for its receive addr 0.
    from app.services import descriptor as d
    desc_str = f"wsh(sortedmulti(2,{BIP84_ZPUB}/<0;1>/*,{BIP84_ZPUB}/<0;1>/*))"
    desc = d.parse_descriptor(desc_str)
    addr0 = d.address_at(desc, 0, 0)
    mock = MockElectrum(
        {scripthash(addr0): [{"tx_hash": "mtx1", "height": 800000}]},
        {"mtx1": {"vin": [], "vout": [{"value": 0.05, "scriptPubKey": {"address": addr0}}],
                  "blocktime": 1700000000}},
    )
    res = xpub.scan_descriptor(mock, desc_str, gap_limit=2)
    assert res.error == "" and res.script_type.startswith("p2wsh")
    assert len(res.txs) == 1 and res.txs[0].net_sats == 5_000_000  # 0.05 BTC received


def test_import_xpub_standalone_creates_buy_sell(session):
    # Standalone (default): external receive = BUY, external send = SELL (taxable).
    a = acc.create_account(session, name="ColdStorage")
    w = acc.add_wallet(session, account_id=a.id, label="zpub wallet",
                       wtype="xpub", xpub=BIP84_ZPUB, gap_limit=2)
    client = _build_mock()
    res = xpub.import_xpub(session, wallet=w, client=client)
    assert res.errors == []
    assert res.imported == 2
    kinds = {t.kind for t in tx_svc.list_transactions(session, a.id)}
    assert kinds == {TxKind.BUY, TxKind.SELL}
    # idempotent re-import (matched by direction-keyed external_id)
    res2 = xpub.import_xpub(session, wallet=w, client=client)
    assert res2.imported == 0 and res2.skipped == 2


def test_import_xpub_custodial_fed_creates_transfers(session):
    # custodial_fed: external moves stay transfers (basis from the exchange CSV side).
    a = acc.create_account(session, name="ColdStorage2")
    w = acc.add_wallet(session, account_id=a.id, label="zpub wallet", wtype="xpub",
                       xpub=BIP84_ZPUB, gap_limit=2, onchain_mode="custodial_fed")
    res = xpub.import_xpub(session, wallet=w, client=_build_mock())
    kinds = {t.kind for t in tx_svc.list_transactions(session, a.id)}
    assert kinds == {TxKind.TRANSFER_IN, TxKind.TRANSFER_OUT}


def test_wallet_edit_and_delete_routes(client):
    import re
    from sqlalchemy import select
    from app.db import SessionLocal
    from app.models import Account
    client.post("/accounts", data={"name": "WalletEditAcct"})
    with SessionLocal() as s:
        aid = s.scalar(select(Account.id).where(Account.name == "WalletEditAcct"))
    r = client.post(f"/accounts/{aid}/wallets", data={"label": "W1", "xpub": BIP84_ZPUB, "gap_limit": "5"})
    wid = re.search(r"/wallets/(\d+)/sync", r.text).group(1)
    # edit label
    r2 = client.post(f"/wallets/{wid}/edit", data={"label": "W1-renamed", "xpub": BIP84_ZPUB, "gap_limit": "10"})
    assert r2.status_code == 200 and "W1-renamed" in r2.text
    assert "W1-renamed" in client.get(f"/wallets/{wid}/edit-form").text
    # invalid xpub rejected
    r3 = client.post(f"/wallets/{wid}/edit", data={"label": "W1-renamed", "xpub": "notanxpub", "gap_limit": "5"})
    assert "Unrecognized" in r3.text
    # delete
    r4 = client.post(f"/wallets/{wid}/delete")
    assert r4.status_code == 200 and "W1-renamed" not in r4.text


def test_add_xpub_wallet_and_sync_routes(client):
    client.post("/accounts", data={"name": "XpubAcct"})
    import re
    aid = re.search(r"/accounts/(\d+)", client.get("/accounts").text).group(1)
    # add an xpub wallet
    r = client.post(f"/accounts/{aid}/wallets",
                    data={"label": "Coldcard", "xpub": BIP84_ZPUB, "gap_limit": "5"})
    assert r.status_code == 200
    assert "Coldcard" in r.text
    # sync with no Electrum configured -> graceful error banner, still 200
    detail = client.get(f"/accounts/{aid}").text
    wid = re.search(r"/wallets/(\d+)/sync", detail).group(1)
    s = client.post(f"/wallets/{wid}/sync")
    assert s.status_code == 200
    # No usable node in tests -> graceful error banner (either "no server configured"
    # or a connection failure), never a crash.
    assert "electrum" in s.text.lower()
