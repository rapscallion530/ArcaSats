# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Parse a RAW Bitcoin transaction (hex) into the same shape the xpub scanner expects from a
verbose Electrum `blockchain.transaction.get`. Fallback for Electrum servers that don't support
verbose transactions (e.g. blockstream's public electrs answers "verbose transactions are
currently unsupported"). Handles legacy + segwit serialization; decodes each output's
scriptPubKey to an address via script.scriptpubkey_to_address.

The raw wire format carries no block time, so the returned dict has no `blocktime` — the caller
supplies the date from the block header at the tx's height (see importers/xpub.py)."""
from __future__ import annotations

from decimal import Decimal

from app.models import SATS_PER_BTC
from app.services.script import scriptpubkey_to_address


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        b = self.data[self.pos:self.pos + n]
        if len(b) != n:
            raise ValueError("raw tx truncated")
        self.pos += n
        return b

    def u(self, n: int) -> int:
        return int.from_bytes(self.take(n), "little")

    def varint(self) -> int:
        i = self.u(1)
        if i < 0xFD:
            return i
        return self.u({0xFD: 2, 0xFE: 4}.get(i, 8))


def parse_raw_tx(raw_hex: str, network: str = "mainnet") -> dict:
    """Return {"vin": [{"txid","vout"}], "vout": [{"n","value"(BTC),"scriptPubKey":{"hex","address"}}]}
    — verbose-compatible for the scanner. `txid` values are display order (big-endian hex)."""
    r = _Reader(bytes.fromhex(raw_hex))
    r.u(4)  # version
    # BIP144 segwit marker+flag (0x00 0x01) — only present when there's witness data.
    segwit = r.data[r.pos:r.pos + 2] == b"\x00\x01"
    if segwit:
        r.take(2)

    vin = []
    for _ in range(r.varint()):
        prev_txid = r.take(32)[::-1].hex()   # internal (LE) -> display (BE)
        prev_vout = r.u(4)
        r.take(r.varint())                   # scriptSig (unused)
        r.u(4)                               # sequence
        vin.append({"txid": prev_txid, "vout": prev_vout})

    vout = []
    for i in range(r.varint()):
        value_sats = r.u(8)
        spk = r.take(r.varint())
        addr = scriptpubkey_to_address(spk, network)
        spk_dict = {"hex": spk.hex()}
        if addr:
            spk_dict["address"] = addr
        vout.append({"n": i, "value": Decimal(value_sats) / SATS_PER_BTC, "scriptPubKey": spk_dict})

    # (witness stacks + locktime follow but aren't needed for scanning.)
    return {"vin": vin, "vout": vout}
