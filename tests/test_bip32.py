"""Validate the pure-Python crypto primitives against known test vectors."""
from app.services.bip32 import hash160, ripemd160
from app.services.script import b58check_encode, segwit_encode


def test_ripemd160_vectors():
    assert ripemd160(b"").hex() == "9c1185a5c5e9fc54612808977ee8f548b2258d31"
    assert ripemd160(b"abc").hex() == "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"
    assert ripemd160(b"message digest").hex() == "5d0689ef49d2fae572b881b123a85ffa21595f36"


def test_base58check_p2pkh_vector():
    # hash160 of the genesis coinbase pubkey -> the genesis address.
    h = bytes.fromhex("62e907b15cbf27d5425399ebf6f0fb50ebb88f18")
    assert b58check_encode(b"\x00" + h) == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def test_bech32_encode_vector():
    prog = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
    assert segwit_encode("bc", 0, prog) == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


def test_hash160_compose():
    # hash160(pubkey) used everywhere; sanity that it's ripemd160(sha256(x))
    import hashlib
    x = b"\x02" + b"\x11" * 32
    assert hash160(x) == ripemd160(hashlib.sha256(x).digest())
