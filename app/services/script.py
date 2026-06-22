# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Bitcoin address -> scriptPubKey -> Electrum scripthash.

Self-contained address decoders (bech32/bech32m + base58check) so we can compute
the scripthash an Electrum server (electrs/Fulcrum) indexes by, without depending
on ripemd160 availability in hashlib.
"""
from __future__ import annotations

import hashlib

# --- base58 ------------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58_encode(b: bytes) -> str:
    num = int.from_bytes(b, "big")
    enc = ""
    while num > 0:
        num, rem = divmod(num, 58)
        enc = _B58[rem] + enc
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + enc


def b58check_encode(payload: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return b58_encode(payload + chk)


def b58check_decode(s: str) -> bytes:
    num = 0
    for ch in s:
        num = num * 58 + _B58.index(ch)
    # account for leading '1's -> 0x00 bytes
    pad = len(s) - len(s.lstrip("1"))
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    full = b"\x00" * pad + body
    payload, checksum = full[:-4], full[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        raise ValueError("bad base58 checksum")
    return payload


# --- bech32 / bech32m (BIP173 / BIP350 reference) ----------------------------
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _hrp_expand(hrp):
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_decode(bech):
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return None, None, None
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        return None, None, None
    hrp = bech[:pos]
    try:
        data = [_CHARSET.index(x) for x in bech[pos + 1:]]
    except ValueError:
        return None, None, None
    const = _polymod(_hrp_expand(hrp) + data)
    if const == _BECH32_CONST:
        spec = "bech32"
    elif const == _BECH32M_CONST:
        spec = "bech32m"
    else:
        return None, None, None
    return hrp, data[:-6], spec


def _convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _bech32_create_checksum(hrp, data, spec):
    const = _BECH32M_CONST if spec == "bech32m" else _BECH32_CONST
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0, 0, 0, 0, 0, 0]) ^ const
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _bech32_encode(hrp, data, spec):
    combined = data + _bech32_create_checksum(hrp, data, spec)
    return hrp + "1" + "".join(_CHARSET[d] for d in combined)


def segwit_encode(hrp: str, witver: int, witprog: bytes) -> str:
    spec = "bech32" if witver == 0 else "bech32m"
    data = [witver] + _convertbits(list(witprog), 8, 5)
    return _bech32_encode(hrp, data, spec)


def _segwit_decode(addr):
    hrp, data, spec = _bech32_decode(addr)
    if hrp is None or hrp not in ("bc", "tb", "bcrt") or not data:
        return None
    witver = data[0]
    decoded = _convertbits(data[1:], 5, 8, False)
    if decoded is None:
        return None
    # BIP173/BIP350 validity: version 0..16; program 2..40 bytes; v0 must be exactly 20
    # (p2wpkh) or 32 (p2wsh); v0 uses bech32, v1+ uses bech32m. Reject anything else so we
    # never derive a scriptPubKey/scripthash from a malformed-but-checksum-valid address.
    if not (0 <= witver <= 16):
        return None
    if not (2 <= len(decoded) <= 40):
        return None
    if witver == 0 and len(decoded) not in (20, 32):
        return None
    if witver == 0 and spec != "bech32":
        return None
    if witver != 0 and spec != "bech32m":
        return None
    return witver, bytes(decoded)


# --- public API --------------------------------------------------------------
def address_to_scriptpubkey(addr: str) -> bytes:
    """Return the scriptPubKey bytes for a BTC address (p2pkh/p2sh/p2wpkh/p2tr)."""
    seg = _segwit_decode(addr)
    if seg is not None:
        witver, program = seg
        op = 0x00 if witver == 0 else (0x50 + witver)  # OP_0 / OP_1..16
        return bytes([op, len(program)]) + program
    payload = b58check_decode(addr)
    version, h = payload[0], payload[1:]
    if version in (0x00, 0x6F):  # p2pkh mainnet/testnet
        return b"\x76\xa9\x14" + h + b"\x88\xac"
    if version in (0x05, 0xC4):  # p2sh mainnet/testnet
        return b"\xa9\x14" + h + b"\x87"
    raise ValueError(f"unsupported address version: {version}")


def scripthash(addr: str) -> str:
    """Electrum scripthash: sha256(scriptPubKey), byte-reversed, hex."""
    spk = address_to_scriptpubkey(addr)
    return hashlib.sha256(spk).digest()[::-1].hex()
