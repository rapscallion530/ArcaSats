"""Multisig output-descriptor parsing + address derivation."""
import hashlib

import pytest

from app.services import descriptor as d
from app.services.script import _segwit_decode, segwit_encode

# Known-valid BIP84 account extended key (same vector used in test_xpub). Using one valid key
# (repeated for a 2-of-2) keeps the test self-contained; sorting is exercised with two distinct
# child pubkeys derived from it.
ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868Rv"
        "UUkgDKf31mGDtKsAYz2oz2AGutZYs")
DESC = (f"wsh(sortedmulti(2,[aaaaaaaa/48h/0h/0h/2h]{ZPUB}/<0;1>/*,"
        f"[bbbbbbbb/48h/0h/0h/2h]{ZPUB}/<0;1>/*))")


def test_p2wsh_address_encoding_bip173_vector():
    # BIP173 reference: a 32-byte v0 program encodes to this P2WSH address.
    prog = bytes.fromhex("1863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262")
    assert segwit_encode("bc", 0, prog) == \
        "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"


def test_is_descriptor():
    assert d.is_descriptor(DESC)
    assert d.is_descriptor("sh(wsh(sortedmulti(2,xpubA/0/*,xpubB/0/*)))")
    assert not d.is_descriptor(ZPUB)                  # a bare key is not a descriptor
    assert not d.is_descriptor("xpub6abc...")


def test_parse_descriptor():
    desc = d.parse_descriptor(DESC + "#checksum1")
    assert desc.kind == "p2wsh"
    assert desc.m == 2 and desc.n == 2
    assert desc.sortedkeys is True
    assert desc.chains == [0, 1]                      # from /<0;1>/*
    assert desc.xpubs == [ZPUB, ZPUB]                 # origin [..] stripped
    assert desc.network == "mainnet"


def test_witness_script_is_bip67_sorted_2of2():
    desc = d.parse_descriptor(DESC)
    pk_a, pk_b = d.derive_pubkey(ZPUB, 0, 0), d.derive_pubkey(ZPUB, 0, 1)  # two distinct pubkeys
    assert pk_a != pk_b
    script = d.witness_script_for(desc, [pk_a, pk_b])
    lo, hi = sorted([pk_a, pk_b])                     # BIP67: ascending by raw bytes
    assert script == bytes([0x52]) + b"\x21" + lo + b"\x21" + hi + bytes([0x52, 0xae])


def test_multisig_address_is_valid_p2wsh_and_deterministic():
    desc = d.parse_descriptor(DESC)
    addr = d.address_at(desc, 0, 0)
    assert addr.startswith("bc1q") and len(addr) == 62          # P2WSH (32-byte program)
    # Round-trip: the address's witness program must equal sha256 of the assembled script.
    witver, program = _segwit_decode(addr)
    expected = hashlib.sha256(
        d.witness_script_for(desc, [d.derive_pubkey(x, 0, 0) for x in desc.xpubs])).digest()
    assert witver == 0 and program == expected
    assert d.address_at(desc, 0, 0) == addr                     # deterministic
    assert d.address_at(desc, 0, 1) != addr                     # different index -> different addr


def test_parse_rejects_garbage():
    for bad in ("wsh(pkh(xpub6abc/0/*))", f"wsh(sortedmulti(2,{ZPUB}/0/*))", "notadescriptor"):
        with pytest.raises(ValueError):
            d.parse_descriptor(bad)


def test_named_cosigner_policy_gives_helpful_error():
    # Sparrow's *script policy* view substitutes short cosigner LABELS for the xpubs (which
    # live elsewhere in the wallet file). It still has descriptor SHAPE, so we must reject it
    # with a hint pointing at the Output Descriptor export — not a cryptic prefix error.
    from app.services import accounts as acc
    policy = "wsh(sortedmulti(2,A,B,C))"
    assert d.is_descriptor(policy)                       # looks like a descriptor by shape
    with pytest.raises(ValueError, match="cosigner label"):
        d.parse_descriptor(policy)
    # the wallet-form validator surfaces the same guidance to the user
    err = acc.validate_key_or_descriptor(policy)
    assert "Output Descriptor" in err
