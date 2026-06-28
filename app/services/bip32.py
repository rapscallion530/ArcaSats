# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Pure-Python BIP32 watch-only derivation (no native deps).

Derives receive/change addresses from an account-level extended PUBLIC key
(xpub/ypub/zpub + testnet tpub/upub/vpub). Includes a self-contained RIPEMD-160
and minimal secp256k1 so it works on any Python (incl. the StartOS container)
without coincurve/bip-utils. Validated against BIP84/BIP173 test vectors.
"""
from __future__ import annotations

import hashlib
import hmac

from app.services.script import b58check_decode, b58check_encode, segwit_encode

# --- RIPEMD-160 (pure python) ------------------------------------------------
_RL = [
    [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8],
    [7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12],
    [11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5],
    [11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12],
    [9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6],
]
_RR = [
    [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6],
    [9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11],
    [9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5],
    [15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8],
    [8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11],
]
_OL = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    [7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8],
    [3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12],
    [1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2],
    [4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13],
]
_OR = [
    [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12],
    [6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2],
    [15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13],
    [8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14],
    [12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11],
]
_KL = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
_KR = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]
_MASK = 0xFFFFFFFF


def _rol(x, n):
    return ((x << n) | (x >> (32 - n))) & _MASK


def _f(j, x, y, z):
    if j < 16:
        return x ^ y ^ z
    if j < 32:
        return (x & y) | (~x & z)
    if j < 48:
        return (x | (~y & _MASK)) ^ z
    if j < 64:
        return (x & z) | (y & ~z & _MASK)
    return x ^ (y | (~z & _MASK))


def ripemd160(message: bytes) -> bytes:
    h0, h1, h2, h3, h4 = 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0
    msg = bytearray(message)
    ml = (8 * len(message)) & 0xFFFFFFFFFFFFFFFF
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += ml.to_bytes(8, "little")

    for off in range(0, len(msg), 64):
        X = [int.from_bytes(msg[off + 4 * i: off + 4 * i + 4], "little") for i in range(16)]
        al, bl, cl, dl, el = h0, h1, h2, h3, h4
        ar, br, cr, dr, er = h0, h1, h2, h3, h4
        for j in range(80):
            rnd = j // 16
            t = (_rol((al + _f(j, bl, cl, dl) + X[_OL[rnd][j % 16]] + _KL[rnd]) & _MASK, _RL[rnd][j % 16]) + el) & _MASK
            al, el, dl, cl, bl = el, dl, _rol(cl, 10), bl, t
            t = (_rol((ar + _f(79 - j, br, cr, dr) + X[_OR[rnd][j % 16]] + _KR[rnd]) & _MASK, _RR[rnd][j % 16]) + er) & _MASK
            ar, er, dr, cr, br = er, dr, _rol(cr, 10), br, t
        t = (h1 + cl + dr) & _MASK
        h1 = (h2 + dl + er) & _MASK
        h2 = (h3 + el + ar) & _MASK
        h3 = (h4 + al + br) & _MASK
        h4 = (h0 + bl + cr) & _MASK
        h0 = t
    return b"".join(x.to_bytes(4, "little") for x in (h0, h1, h2, h3, h4))


def hash160(b: bytes) -> bytes:
    return ripemd160(hashlib.sha256(b).digest())


# --- secp256k1 (minimal) -----------------------------------------------------
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
_G = (_GX, _GY)


def _inv(x):
    return pow(x, _P - 2, _P)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % _P == 0:
        return None
    if p == q:
        m = (3 * p[0] * p[0]) * _inv(2 * p[1]) % _P
    else:
        m = (q[1] - p[1]) * _inv(q[0] - p[0]) % _P
    x = (m * m - p[0] - q[0]) % _P
    y = (m * (p[0] - x) - p[1]) % _P
    return (x, y)


# Scalar multiplication in JACOBIAN coordinates (X, Y, Z) with affine x=X/Z^2, y=Y/Z^3.
# This defers the expensive modular inverse to ONE per scalar-mul instead of one per point
# addition (~256 per mul in the old affine double-and-add) — a large speedup for address scans.
# secp256k1 has a=0, which simplifies the doubling formula.
def _jac_double(pt):
    x1, y1, z1 = pt
    if y1 == 0 or z1 == 0:
        return (0, 0, 0)  # point at infinity
    s = (4 * x1 * y1 * y1) % _P
    m = (3 * x1 * x1) % _P
    x3 = (m * m - 2 * s) % _P
    y3 = (m * (s - x3) - 8 * pow(y1, 4, _P)) % _P
    z3 = (2 * y1 * z1) % _P
    return (x3, y3, z3)


def _jac_add(p, q):
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1z1 = z1 * z1 % _P
    z2z2 = z2 * z2 % _P
    u1 = x1 * z2z2 % _P
    u2 = x2 * z1z1 % _P
    s1 = y1 * z2 * z2z2 % _P
    s2 = y2 * z1 * z1z1 % _P
    if u1 == u2:
        return _jac_double(p) if s1 == s2 else (0, 0, 0)
    h = (u2 - u1) % _P
    r = (s2 - s1) % _P
    hh = h * h % _P
    hhh = h * hh % _P
    v = u1 * hh % _P
    x3 = (r * r - hhh - 2 * v) % _P
    y3 = (r * (v - x3) - s1 * hhh) % _P
    z3 = (z1 * z2 * h) % _P
    return (x3, y3, z3)


def _mul(k, p):
    """Affine point p (x, y) or None, times scalar k. Returns affine point or None."""
    if p is None or k == 0:
        return None
    r = (0, 0, 0)            # infinity (Jacobian)
    q = (p[0], p[1], 1)      # p in Jacobian
    while k:
        if k & 1:
            r = _jac_add(r, q)
        q = _jac_double(q)
        k >>= 1
    if r[2] == 0:
        return None
    zinv = _inv(r[2])
    zinv2 = zinv * zinv % _P
    return (r[0] * zinv2 % _P, r[1] * zinv2 * zinv % _P)


def _decompress(pub: bytes):
    prefix, x = pub[0], int.from_bytes(pub[1:33], "big")
    if x >= _P:
        raise ValueError("invalid point: x out of field range")
    alpha = (x * x * x + 7) % _P
    y = pow(alpha, (_P + 1) // 4, _P)
    if (y * y) % _P != alpha:
        raise ValueError("invalid public key: point not on curve")  # tamper / corruption guard
    if (y & 1) != (prefix & 1):
        y = _P - y
    return (x, y)


def _compress(point) -> bytes:
    x, y = point
    return bytes([0x02 | (y & 1)]) + x.to_bytes(32, "big")


# --- BIP32 -------------------------------------------------------------------
def _deserialize(ext: str):
    raw = b58check_decode(ext)
    if len(raw) != 78:
        raise ValueError("bad extended key length")
    return raw[13:45], raw[45:78]  # chaincode, keydata(33, compressed pubkey)


def _ckd_pub(pubkey: bytes, chaincode: bytes, index: int):
    if index >= 0x80000000:
        raise ValueError("cannot derive hardened child from public key")
    I = hmac.new(chaincode, pubkey + index.to_bytes(4, "big"), hashlib.sha512).digest()
    il = int.from_bytes(I[:32], "big")
    if il >= _N:
        raise ValueError("invalid child (il >= n)")
    point = _add(_mul(il, _G), _decompress(pubkey))
    if point is None:
        raise ValueError("invalid child (point at infinity)")
    return _compress(point), I[32:]


_PREFIX = {
    "xpub": ("p2pkh", "mainnet"), "ypub": ("p2sh-p2wpkh", "mainnet"), "zpub": ("p2wpkh", "mainnet"),
    "tpub": ("p2pkh", "testnet"), "upub": ("p2sh-p2wpkh", "testnet"), "vpub": ("p2wpkh", "testnet"),
}


def key_kind(ext: str) -> tuple[str, str]:
    pre = ext[:4].lower()
    if pre not in _PREFIX:
        raise ValueError(f"unrecognized extended key prefix: {pre}")
    return _PREFIX[pre]


def pubkey_to_address(pubkey: bytes, script_type: str, network: str) -> str:
    h = hash160(pubkey)
    testnet = network == "testnet"
    if script_type == "p2wpkh":
        return segwit_encode("tb" if testnet else "bc", 0, h)
    if script_type == "p2sh-p2wpkh":
        redeem_h = hash160(b"\x00\x14" + h)
        return b58check_encode(bytes([0xC4 if testnet else 0x05]) + redeem_h)
    # p2pkh
    return b58check_encode(bytes([0x6F if testnet else 0x00]) + h)


def derive_pubkey(xpub: str, change: int, index: int) -> bytes:
    """The 33-byte compressed child public key at change/index (for multisig descriptors,
    where we need the raw pubkeys to assemble the m-of-n witness script, not an address)."""
    chaincode, key = _deserialize(xpub)
    ck, cc = _ckd_pub(key, chaincode, change)
    leaf, _ = _ckd_pub(ck, cc, index)
    return leaf


def derive_addresses(xpub: str, change: int = 0, count: int = 20, start: int = 0,
                     script_type: str | None = None) -> list[tuple[int, str]]:
    """Derive addresses. The extended-key prefix sets the default script type, but it
    can be overridden — the same pubkey tree yields p2pkh/p2sh-p2wpkh/p2wpkh addresses,
    so a plain `xpub` holding segwit coins can still be scanned (see scan auto-detect)."""
    default_type, network = key_kind(xpub)
    st = script_type or default_type
    chaincode, key = _deserialize(xpub)
    ck, cc = _ckd_pub(key, chaincode, change)  # change-level node
    out = []
    for i in range(start, start + count):
        leaf, _ = _ckd_pub(ck, cc, i)
        out.append((i, pubkey_to_address(leaf, st, network)))
    return out
