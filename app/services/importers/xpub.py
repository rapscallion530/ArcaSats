# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""xpub (watch-only) on-chain import via an Electrum server.

Derives addresses from an account-level extended public key (xpub/ypub/zpub and
testnet tpub/upub/vpub), scans them against an Electrum server with a gap limit,
and records net on-chain movements as transfer_in / transfer_out transactions.

Classification depends on the wallet's `onchain_mode` (see docs/onchain-classification.md):
a true transfer requires BOTH counterparties to be wallets we've loaded. For a "standalone"
wallet, an external inflow is a BUY and an external outflow a SELL (the dangerous old default
was to call everything a non-taxable transfer, hiding taxable events). Genuine transfers
between your own loaded wallets are restored by reconcile_internal_transfers() via shared-txid
matching. (Change outputs are already netted within each tx by scan_xpub — both the receive
and change chains are derived, so coins returning to us don't count as a separate inflow.)
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SATS_PER_BTC, Transaction, TxKind, Wallet
from app.services import transactions as tx_svc
from app.services.bip32 import derive_addresses, key_kind
from app.services.electrum import ElectrumLike
from app.services.script import scripthash

# Probe order: native segwit first (most common today), then wrapped, then legacy.
_SCRIPT_TYPES = ("p2wpkh", "p2sh-p2wpkh", "p2pkh")


def detect_script_type(client: ElectrumLike, xpub: str, probe: int = 20) -> str:
    """Find which address encoding actually has on-chain history. Probes the first `probe`
    addresses of BOTH the receive and change chains for each script type — NOT just index 0,
    whose receive address may be unused (a gap), which would misdetect the wallet and scan the
    wrong addresses (e.g. an xpub-prefixed native-segwit wallet falling back to p2pkh and
    importing nothing). Falls back to the prefix default only if nothing is found."""
    for st in _SCRIPT_TYPES:
        for chain in (0, 1):
            for _idx, addr in derive_addresses(xpub, change=chain, count=probe, start=0, script_type=st):
                if client.get_history(scripthash(addr)):
                    return st
    return key_kind(xpub)[0]


@dataclass
class OnChainTx:
    txid: str
    timestamp: dt.datetime
    net_sats: int           # +received / -sent (net of change)
    height: int
    address: str = ""


@dataclass
class ScanResult:
    txs: list[OnChainTx] = field(default_factory=list)
    addresses_scanned: int = 0
    error: str = ""
    script_type: str = ""


def _val_to_sats(value) -> int:
    return int((Decimal(str(value)) * SATS_PER_BTC).to_integral_value(rounding="ROUND_HALF_UP"))


def _vout_address(vout: dict) -> str | None:
    spk = vout.get("scriptPubKey", {})
    if "address" in spk:
        return spk["address"]
    addrs = spk.get("addresses")
    if addrs:
        return addrs[0]
    return None


def _scan_addresses(client: ElectrumLike, derive_one, chains, gap_limit: int,
                    max_per_chain: int, result: ScanResult) -> None:
    """Walk each chain with a gap limit using `derive_one(chain, index) -> address`, collect
    the wallet's addresses + touching txids, then compute each tx's net (received - sent) from
    its verbose vin/vout. Address-type-agnostic — works for single-sig and multisig alike."""
    our_addrs: set[str] = set()
    txid_height: dict[str, int] = {}
    for chain in chains:
        unused_run = 0
        i = 0
        while unused_run < gap_limit and i < max_per_chain:
            addr = derive_one(chain, i)
            hist = client.get_history(scripthash(addr))
            our_addrs.add(addr)
            result.addresses_scanned += 1
            if hist:
                unused_run = 0
                for h in hist:
                    txid_height[h["tx_hash"]] = h.get("height", 0)
            else:
                unused_run += 1
            i += 1

    tx_cache: dict[str, dict] = {}

    def get_tx(txid: str) -> dict:
        if txid not in tx_cache:
            tx_cache[txid] = client.get_transaction(txid, verbose=True)
        return tx_cache[txid]

    for txid, height in txid_height.items():
        tx = get_tx(txid)
        received = 0
        rep_addr = ""
        for vout in tx.get("vout", []):
            addr = _vout_address(vout)
            if addr in our_addrs:
                received += _val_to_sats(vout.get("value", 0))
                rep_addr = rep_addr or addr
        sent = 0
        for vin in tx.get("vin", []):
            ptxid = vin.get("txid")
            # Only inputs spending one of OUR prior txs can be ours; skipping the rest avoids
            # fetching thousands of unrelated txs over Tor.
            if not ptxid or ptxid not in txid_height:
                continue
            prev = get_tx(ptxid)
            pv = prev.get("vout", [])
            idx = vin.get("vout", 0)
            if idx < len(pv):
                paddr = _vout_address(pv[idx])
                if paddr in our_addrs:
                    sent += _val_to_sats(pv[idx].get("value", 0))
                    rep_addr = rep_addr or paddr
        net = received - sent
        if net == 0:
            continue
        ts_unix = tx.get("blocktime") or tx.get("time")
        ts = (dt.datetime.fromtimestamp(ts_unix, dt.UTC).replace(tzinfo=None)
              if ts_unix else dt.datetime.now(dt.UTC).replace(tzinfo=None))
        result.txs.append(OnChainTx(txid=txid, timestamp=ts, net_sats=net, height=height, address=rep_addr))


def scan_xpub(
    client: ElectrumLike, xpub: str, *, gap_limit: int = 20, max_per_chain: int = 1000,
    script_type: str | None = None,
) -> ScanResult:
    """Scan a single-sig xpub. script_type=None auto-detects the address encoding."""
    result = ScanResult()
    try:
        if script_type is None:
            script_type = detect_script_type(client, xpub)
        result.script_type = script_type

        def derive_one(chain, i):
            return derive_addresses(xpub, change=chain, count=1, start=i, script_type=script_type)[0][1]

        _scan_addresses(client, derive_one, (0, 1), gap_limit, max_per_chain, result)
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    return result


def scan_descriptor(
    client: ElectrumLike, descriptor_str: str, *, gap_limit: int = 20, max_per_chain: int = 1000,
) -> ScanResult:
    """Scan a multisig output descriptor (wsh/sh/sh(wsh) of (sorted)multi). Derives the quorum's
    addresses per index and scans them exactly like an xpub."""
    from app.services import descriptor as desc_mod
    result = ScanResult()
    try:
        desc = desc_mod.parse_descriptor(descriptor_str)
        result.script_type = f"{desc.kind} {desc.m}-of-{desc.n}"

        def derive_one(chain, i):
            return desc_mod.address_at(desc, chain, i)

        _scan_addresses(client, derive_one, tuple(desc.chains), gap_limit, max_per_chain, result)
    except Exception as exc:  # noqa: BLE001
        result.error = str(exc)
    return result


def import_xpub(session: Session, *, wallet: Wallet, client: ElectrumLike, gap_limit: int | None = None):
    """Scan a wallet (single-sig xpub OR multisig descriptor) and persist its on-chain activity.
    Returns an ImportResult (imported / skipped / errors)."""
    from app.services import descriptor as desc_mod
    from app.services.importers.csv_import import ImportResult

    res = ImportResult()
    gap = gap_limit or wallet.gap_limit
    key = wallet.xpub or ""
    if desc_mod.is_descriptor(key):
        scan = scan_descriptor(client, key, gap_limit=gap)
    else:
        # Honor a user address-type override (Settings); else auto-detect.
        override = (wallet.address_type or "auto")
        st = None if override in ("", "auto") else override
        scan = scan_xpub(client, key, gap_limit=gap, script_type=st)
    if scan.error:
        res.errors.append(scan.error)
        return res
    # Record the detected script type for display.
    if scan.script_type and wallet.script_type != scan.script_type:
        wallet.script_type = scan.script_type
        session.commit()

    mode = wallet.onchain_mode or "standalone"
    src = f"xpub:{wallet.id}"
    for oc in scan.txs:
        inflow = oc.net_sats > 0
        direction = "in" if inflow else "out"
        if mode == "custodial_fed":
            kind = TxKind.TRANSFER_IN if inflow else TxKind.TRANSFER_OUT
        else:  # standalone: external inflow = buy, outflow = sell (taxable; USD via price feed).
            kind = TxKind.BUY if inflow else TxKind.SELL
        # Dedupe id keyed on DIRECTION (not kind) so a re-sync after a reclassification matches
        # the existing row instead of inserting a duplicate.
        ext = f"{oc.txid}:{direction}"
        note = f"block {oc.height}" if oc.height else "mempool"
        existing = session.scalar(select(Transaction).where(
            Transaction.account_id == wallet.account_id, Transaction.source == src,
            Transaction.external_id == ext))
        if existing is not None:
            # Re-sync: refresh the on-chain facts but PRESERVE kind — it may have been
            # reclassified (cross-wallet transfer) or edited by the user.
            existing.amount_sats = abs(oc.net_sats)
            existing.timestamp = oc.timestamp
            existing.address = oc.address
            existing.note = note
            session.commit()
            res.skipped += 1
            continue
        tx = tx_svc.add_transaction(
            session, account_id=wallet.account_id, wallet_id=wallet.id, kind=kind,
            timestamp=oc.timestamp, amount_sats=abs(oc.net_sats), txid=oc.txid,
            address=oc.address, counterparty="on-chain", source=src,
            external_id=ext, note=note,
        )
        if tx is None:
            res.skipped += 1
        else:
            res.imported += 1
    return res
