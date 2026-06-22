# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Multisig output-descriptor support (watch-only).

Parses a wallet output descriptor as exported by Sparrow / Unchained, e.g.

    wsh(sortedmulti(2,[fp/48h/0h/0h/2h]xpubA/<0;1>/*,[..]xpubB/<0;1>/*,[..]xpubC/<0;1>/*))#cksum

and derives the wallet's addresses: for each (chain, index) it derives every cosigner's child
pubkey, BIP67-sorts them (for `sortedmulti`), assembles the m-of-n witness script, and wraps
it as P2WSH / P2SH / P2SH-P2WSH. A single xpub cannot reproduce these addresses — you need the
whole quorum + the script, which is exactly what the descriptor encodes.

Crypto primitives are reused from bip32 (BIP32 child derivation, RIPEMD-160) and script
(base58check, bech32). No private keys are ever involved.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.services.bip32 import derive_pubkey, hash160, key_kind
from app.services.script import b58check_encode, segwit_encode

# wrapper -> (kind label). Innermost is always (sorted)multi.
_WRAPPERS = ("sh(wsh(", "wsh(", "sh(")


@dataclass
class MultisigDescriptor:
    kind: str           # "p2wsh" | "p2sh" | "p2sh-p2wsh"
    m: int              # required signatures
    xpubs: list[str]    # cosigner account xpubs (origin/path stripped)
    sortedkeys: bool    # sortedmulti (BIP67) vs multi (preserve order)
    chains: list[int]   # receive/change chains to scan, e.g. [0, 1]
    network: str        # "mainnet" | "testnet"

    @property
    def n(self) -> int:
        return len(self.xpubs)


def is_descriptor(s: str) -> bool:
    """True if the string looks like an output descriptor (vs a bare xpub)."""
    s = (s or "").strip().lower()
    return "multi(" in s and any(s.startswith(w) for w in _WRAPPERS)


def _strip_checksum(s: str) -> str:
    return s.split("#", 1)[0].strip()


def _chains_from_path(path: str) -> list[int]:
    """Chains to scan, from the key path after the xpub.

    An explicit multipath ('/<0;1>/*') encodes both chains directly -> [0, 1].

    A single fixed chain ('/0/*') is how Sparrow/Unchained export the *receive*
    descriptor on its own; the matching change descriptor lives on chain 1. If we
    scanned only the literal chain we'd silently miss every change address (and
    undercount balance/history), so a single fixed chain expands to scan both
    receive and change: '/0/*' -> [0, 1], '/1/*' -> [0, 1]. A bare '/*' -> [0, 1].
    """
    m = re.search(r"<\s*(\d+)\s*;\s*(\d+)\s*>", path)
    if m:
        return [int(m.group(1)), int(m.group(2))]
    return [0, 1]


def parse_descriptor(s: str) -> MultisigDescriptor:
    """Parse a (sorted)multi descriptor. Raises ValueError on anything unsupported."""
    raw = _strip_checksum(s)
    low = raw.lower()
    if low.startswith("sh(wsh("):
        kind, inner = "p2sh-p2wsh", raw[len("sh(wsh("):]
        if not inner.endswith("))"):
            raise ValueError("malformed sh(wsh(...)) descriptor")
        inner = inner[:-2]
    elif low.startswith("wsh("):
        kind, inner = "p2wsh", raw[len("wsh("):-1]
    elif low.startswith("sh("):
        kind, inner = "p2sh", raw[len("sh("):-1]
    else:
        raise ValueError("unsupported descriptor wrapper (expected wsh/sh/sh(wsh))")

    inner = inner.strip()
    low = inner.lower()
    if low.startswith("sortedmulti("):
        sortedkeys, body = True, inner[len("sortedmulti("):-1]
    elif low.startswith("multi("):
        sortedkeys, body = False, inner[len("multi("):-1]
    else:
        raise ValueError("descriptor must contain multi(...) or sortedmulti(...)")

    parts = [p.strip() for p in body.split(",")]
    if len(parts) < 3:
        raise ValueError("multisig needs a threshold and at least 2 keys")
    try:
        m = int(parts[0])
    except ValueError as exc:
        raise ValueError("multisig threshold must be an integer") from exc

    xpubs, chains = [], None
    for key in parts[1:]:
        key = re.sub(r"^\[[^\]]*\]", "", key.strip())  # drop [fingerprint/origin] prefix
        mt = re.match(r"([a-zA-Z0-9]+)(/.*)?$", key)
        if not mt:
            raise ValueError(f"unrecognized key in descriptor: {key[:12]}…")
        token = mt.group(1)
        try:
            key_kind(token)  # validate it's a real extended key (raises otherwise)
        except ValueError as exc:
            # Sparrow's *script policy* view substitutes short cosigner LABELS (e.g. A, B, C)
            # for the extended keys, which live elsewhere in the wallet file. Detect that
            # shape (a short token with no derivation path) and point the user at the right
            # export instead of surfacing a cryptic "unrecognized extended key prefix".
            if len(token) <= 12 and not mt.group(2):
                raise ValueError(
                    f"'{token}' is a cosigner label, not an extended key — this looks like a "
                    "Sparrow script policy. In Sparrow use Settings → Export → Output "
                    "Descriptor (it contains the xpubs) and paste that instead."
                ) from None
            raise ValueError(f"unsupported key in descriptor: {exc}") from None
        xpubs.append(token)
        chains = chains or _chains_from_path(mt.group(2) or "")
    if not (1 <= m <= len(xpubs)):
        raise ValueError(f"threshold {m} out of range for {len(xpubs)} keys")
    network = key_kind(xpubs[0])[1]
    return MultisigDescriptor(kind=kind, m=m, xpubs=xpubs, sortedkeys=sortedkeys,
                              chains=chains or [0], network=network)


def _multisig_script(m: int, pubkeys: list[bytes]) -> bytes:
    """OP_m <pk1> ... <pkN> OP_n OP_CHECKMULTISIG (pubkeys already in final order)."""
    parts = [bytes([0x50 + m])]
    for pk in pubkeys:
        parts.append(bytes([len(pk)]) + pk)  # 0x21 + 33-byte compressed pubkey
    parts.append(bytes([0x50 + len(pubkeys)]))
    parts.append(b"\xae")  # OP_CHECKMULTISIG
    return b"".join(parts)


def witness_script_for(desc: MultisigDescriptor, pubkeys: list[bytes]) -> bytes:
    keys = sorted(pubkeys) if desc.sortedkeys else pubkeys  # BIP67: ascending by raw bytes
    return _multisig_script(desc.m, keys)


def _address_from_script(desc: MultisigDescriptor, script: bytes) -> str:
    testnet = desc.network == "testnet"
    if desc.kind == "p2wsh":
        return segwit_encode("tb" if testnet else "bc", 0, hashlib.sha256(script).digest())
    if desc.kind == "p2sh-p2wsh":
        redeem = b"\x00\x20" + hashlib.sha256(script).digest()  # P2WSH inside P2SH
        return b58check_encode(bytes([0xC4 if testnet else 0x05]) + hash160(redeem))
    # p2sh (legacy bare multisig)
    return b58check_encode(bytes([0xC4 if testnet else 0x05]) + hash160(script))


def address_at(desc: MultisigDescriptor, chain: int, index: int) -> str:
    pubkeys = [derive_pubkey(x, chain, index) for x in desc.xpubs]
    return _address_from_script(desc, witness_script_for(desc, pubkeys))


def derive_addresses(desc: MultisigDescriptor, change: int = 0, count: int = 20,
                     start: int = 0) -> list[tuple[int, str]]:
    """(index, address) pairs — same shape as bip32.derive_addresses, for a multisig wallet."""
    return [(i, address_at(desc, change, i)) for i in range(start, start + count)]
