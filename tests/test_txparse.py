"""Raw-transaction parser + scriptPubKey->address decoder (fallback for Electrum servers that
don't support verbose blockchain.transaction.get, e.g. blockstream's electrs)."""
from decimal import Decimal

from app.services import script as S
from app.services import txparse


def test_scriptpubkey_to_address_roundtrip():
    h20, h32 = bytes(range(1, 21)), bytes(range(1, 33))
    cases = [
        (S.b58check_encode(b"\x00" + h20), "mainnet"),   # p2pkh
        (S.b58check_encode(b"\x05" + h20), "mainnet"),   # p2sh
        (S.b58check_encode(b"\x6f" + h20), "testnet"),   # p2pkh testnet
        (S.segwit_encode("bc", 0, h20), "mainnet"),      # p2wpkh
        (S.segwit_encode("bc", 0, h32), "mainnet"),      # p2wsh
        (S.segwit_encode("bc", 1, h32), "mainnet"),      # p2tr (bech32m)
        (S.segwit_encode("tb", 0, h20), "testnet"),      # testnet p2wpkh
    ]
    for addr, net in cases:
        assert S.scriptpubkey_to_address(S.address_to_scriptpubkey(addr), net) == addr


def test_scriptpubkey_to_address_none_for_nonstandard():
    assert S.scriptpubkey_to_address(b"\x6a\x04dead", "mainnet") is None       # OP_RETURN
    assert S.scriptpubkey_to_address(b"", "mainnet") is None


def _legacy_raw(prev_le: bytes, vout_index: int, value_sats: int, spk: bytes) -> str:
    return (b"\x02\x00\x00\x00"                                   # version
            + b"\x01" + prev_le + vout_index.to_bytes(4, "little")  # 1 vin
            + b"\x00" + b"\xff\xff\xff\xff"                        # empty scriptSig + sequence
            + b"\x01" + value_sats.to_bytes(8, "little")          # 1 vout: value
            + bytes([len(spk)]) + spk                             # spk
            + b"\x00\x00\x00\x00").hex()                          # locktime


def test_parse_legacy_tx_decodes_vin_and_vout():
    prev_le = bytes(range(32))
    spk = b"\x00\x14" + bytes([0xAB]) * 20                        # p2wpkh
    tx = txparse.parse_raw_tx(_legacy_raw(prev_le, 3, 150_000, spk), "mainnet")
    assert tx["vin"][0]["txid"] == prev_le[::-1].hex()            # internal LE -> display BE
    assert tx["vin"][0]["vout"] == 3
    assert tx["vout"][0]["value"] == Decimal(150_000) / 100_000_000
    assert tx["vout"][0]["scriptPubKey"]["address"] == S.segwit_encode("bc", 0, bytes([0xAB]) * 20)


def test_parse_segwit_marker_is_skipped():
    # version + segwit marker/flag (00 01) + 1 vin + 1 vout (+ witness/locktime we don't read)
    prev_le = bytes(range(32))
    spk = b"\x00\x14" + bytes([0xCD]) * 20
    raw = (b"\x02\x00\x00\x00" + b"\x00\x01"
           + b"\x01" + prev_le + (0).to_bytes(4, "little") + b"\x00" + b"\xff\xff\xff\xff"
           + b"\x01" + (250_000).to_bytes(8, "little") + bytes([len(spk)]) + spk
           + b"\x01\x00").hex()   # (truncated witness/locktime — parser returns after vout)
    tx = txparse.parse_raw_tx(raw, "mainnet")
    assert tx["vout"][0]["value"] == Decimal(250_000) / 100_000_000
    assert tx["vout"][0]["scriptPubKey"]["address"] == S.segwit_encode("bc", 0, bytes([0xCD]) * 20)


def test_scanner_falls_back_to_raw_when_verbose_unsupported():
    # End-to-end: a server that rejects verbose txs (like blockstream) still yields UTXOs + txs,
    # dated from the block header (not now()).
    from app.services.bip32 import derive_addresses
    from app.services.importers import xpub as X

    ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868Rv"
            "UUkgDKf31mGDtKsAYz2oz2AGutZYs")
    addr0 = derive_addresses(ZPUB, change=0, count=1, script_type="p2wpkh")[0][1]
    spk = S.address_to_scriptpubkey(addr0)
    raw = (b"\x02\x00\x00\x00" + b"\x01" + bytes(range(32)) + (0).to_bytes(4, "little")
           + b"\x00" + b"\xff\xff\xff\xff"
           + b"\x01" + (500_000).to_bytes(8, "little") + bytes([len(spk)]) + spk
           + b"\x00\x00\x00\x00").hex()
    header = (b"\x00" * 68 + (1_609_459_200).to_bytes(4, "little") + b"\x00" * 8).hex()  # 2021-01-01
    txid = "ab" * 32

    class FakeNode:
        def get_history(self, sh):
            return [{"tx_hash": txid, "height": 700_000}] if sh == S.scripthash(addr0) else []

        def get_transaction(self, tid, verbose=True):
            if verbose:
                raise RuntimeError("verbose transactions are currently unsupported")
            return raw

        def block_header(self, height):
            return header

    res = X.scan_xpub(FakeNode(), ZPUB, gap_limit=1, script_type="p2wpkh")
    assert res.error == ""
    assert any(u.address == addr0 and u.value_sats == 500_000 for u in res.utxos)
    assert res.txs and res.txs[0].net_sats == 500_000
    assert res.txs[0].timestamp.year == 2021    # dated from the block header, not now()
