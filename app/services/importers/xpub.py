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

from app.models import SATS_PER_BTC, Account, HopAddress, Transaction, TxKind, Utxo, Wallet
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
class OnChainUtxo:
    """One output paying one of our addresses. spent_txid is None while unspent."""
    txid: str
    vout: int
    value_sats: int
    address: str
    chain: int              # 0 receive, 1 change
    deriv_index: int
    script_type: str = ""
    created_height: int = 0
    created_at: dt.datetime | None = None
    spent_txid: str | None = None
    spent_height: int | None = None
    spent_at: dt.datetime | None = None


@dataclass
class HopEndpoint:
    """A foreign address one hop from one of our txs (for known->unknown->known detection).
    direction "out" = a destination of our outflow; "in" = a funder of our inflow."""
    txid: str
    direction: str
    address: str
    value_sats: int = 0


@dataclass
class ScanResult:
    txs: list[OnChainTx] = field(default_factory=list)
    utxos: list[OnChainUtxo] = field(default_factory=list)
    endpoints: list[HopEndpoint] = field(default_factory=list)
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


def _blocktime(tx: dict) -> dt.datetime:
    ts_unix = tx.get("blocktime") or tx.get("time")
    if ts_unix:
        return dt.datetime.fromtimestamp(ts_unix, dt.UTC).replace(tzinfo=None)
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _scan_addresses(client: ElectrumLike, derive_one, chains, gap_limit: int,
                    max_per_chain: int, result: ScanResult) -> None:
    """Walk each chain with a gap limit using `derive_one(chain, index) -> address`, collect
    the wallet's addresses + touching txids, then derive both the per-output UTXO inventory
    (result.utxos) and each tx's net received-sent (result.txs). Address-type-agnostic — works
    for single-sig and multisig alike."""
    # addr -> (chain, deriv_index); membership in this map == "this address is ours".
    addr_meta: dict[str, tuple[int, int]] = {}
    txid_height: dict[str, int] = {}
    for chain in chains:
        unused_run = 0
        i = 0
        while unused_run < gap_limit and i < max_per_chain:
            addr = derive_one(chain, i)
            hist = client.get_history(scripthash(addr))
            addr_meta[addr] = (chain, i)
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

    # Pass 1 — record every output paying one of our addresses as a (provisionally unspent)
    # UTXO, and tally received-per-tx. Building all outputs first means pass 2 can mark spends
    # regardless of the order txs are visited (a spend may be processed before its funding tx).
    created: dict[tuple[str, int], OnChainUtxo] = {}
    received_by_tx: dict[str, int] = {}
    sent_by_tx: dict[str, int] = {}
    rep_addr: dict[str, str] = {}
    ts_by_tx: dict[str, dt.datetime] = {}
    for txid, height in txid_height.items():
        tx = get_tx(txid)
        ts_by_tx[txid] = _blocktime(tx)
        for vidx, vout in enumerate(tx.get("vout", [])):
            addr = _vout_address(vout)
            meta = addr_meta.get(addr)
            if meta is None:
                continue
            val = _val_to_sats(vout.get("value", 0))
            received_by_tx[txid] = received_by_tx.get(txid, 0) + val
            rep_addr.setdefault(txid, addr)
            created[(txid, vidx)] = OnChainUtxo(
                txid=txid, vout=vidx, value_sats=val, address=addr,
                chain=meta[0], deriv_index=meta[1], script_type=result.script_type,
                created_height=height, created_at=ts_by_tx[txid])

    # Pass 2 — any input spending one of our recorded outputs marks that UTXO spent and counts
    # toward sent. A vin whose prior output we didn't record isn't ours, so it's ignored.
    for txid, height in txid_height.items():
        for vin in get_tx(txid).get("vin", []):
            ptxid = vin.get("txid")
            u = created.get((ptxid, vin.get("vout", 0))) if ptxid else None
            if u is None:
                continue
            sent_by_tx[txid] = sent_by_tx.get(txid, 0) + u.value_sats
            rep_addr.setdefault(txid, u.address)
            u.spent_txid, u.spent_height, u.spent_at = txid, height, ts_by_tx[txid]

    result.utxos = list(created.values())
    for txid, height in txid_height.items():
        net = received_by_tx.get(txid, 0) - sent_by_tx.get(txid, 0)
        if net == 0:
            continue
        result.txs.append(OnChainTx(txid=txid, timestamp=ts_by_tx[txid], net_sats=net,
                                    height=height, address=rep_addr.get(txid, "")))
        # Capture the foreign address one hop away, for address-based "known -> unknown -> known"
        # self-transfer detection (costbasis.suggest_transfers). Inward-only: just the address
        # directly adjacent to our coins, never a deeper walk.
        if net < 0:
            # Outflow: every output NOT paying us is a destination (already-fetched vout — free).
            for vout in get_tx(txid).get("vout", []):
                a = _vout_address(vout)
                if a and a not in addr_meta:
                    result.endpoints.append(HopEndpoint(
                        txid=txid, direction="out", address=a,
                        value_sats=_val_to_sats(vout.get("value", 0))))
        else:
            # Inflow: each input NOT ours is a funder — its address lives in the prev tx's vout,
            # so we fetch that prev tx (a vin only carries txid:vout). Best-effort; skip on error.
            for vin in get_tx(txid).get("vin", []):
                ptxid, pvout = vin.get("txid"), vin.get("vout", 0)
                if not ptxid or created.get((ptxid, pvout)) is not None:
                    continue  # coinbase, or one of our own inputs (already recorded)
                try:
                    pvouts = get_tx(ptxid).get("vout", [])
                except Exception:  # noqa: BLE001 — prev tx unavailable; funder simply unknown
                    continue
                if pvout < len(pvouts):
                    a = _vout_address(pvouts[pvout])
                    if a and a not in addr_meta:
                        result.endpoints.append(HopEndpoint(
                            txid=txid, direction="in", address=a,
                            value_sats=_val_to_sats(pvouts[pvout].get("value", 0))))


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


def persist_utxos(session: Session, wallet: Wallet, utxos: list[OnChainUtxo]) -> None:
    """Upsert the wallet's UTXO inventory, keyed on (wallet, txid, vout). Idempotent: a re-sync
    refreshes spent status (a previously-unspent coin that's now spent) and the provenance label
    without duplicating rows. On-chain history is append-only, so we never delete here."""
    acct = session.get(Account, wallet.account_id)
    label = acct.label_kind if acct else ""
    existing = {
        (u.txid, u.vout): u
        for u in session.scalars(select(Utxo).where(Utxo.wallet_id == wallet.id))
    }
    for oc in utxos:
        row = existing.get((oc.txid, oc.vout))
        if row is None:
            session.add(Utxo(
                account_id=wallet.account_id, wallet_id=wallet.id, txid=oc.txid, vout=oc.vout,
                value_sats=oc.value_sats, address=oc.address, script_type=oc.script_type,
                chain=oc.chain, deriv_index=oc.deriv_index, is_change=(oc.chain == 1),
                label_kind=label, created_height=oc.created_height, created_at=oc.created_at,
                spent_txid=oc.spent_txid, spent_height=oc.spent_height, spent_at=oc.spent_at))
        else:
            row.spent_txid, row.spent_height, row.spent_at = oc.spent_txid, oc.spent_height, oc.spent_at
            row.label_kind = label
    session.commit()


def persist_endpoints(session: Session, wallet: Wallet, endpoints: list[HopEndpoint]) -> None:
    """Upsert the wallet's hop-address endpoints, keyed (wallet, txid, direction, address).
    Idempotent across re-syncs (refreshes value, never duplicates). On-chain history is
    append-only, so we never delete here."""
    existing = {
        (e.txid, e.direction, e.address): e
        for e in session.scalars(select(HopAddress).where(HopAddress.wallet_id == wallet.id))
    }
    for ep in endpoints:
        row = existing.get((ep.txid, ep.direction, ep.address))
        if row is None:
            session.add(HopAddress(
                account_id=wallet.account_id, wallet_id=wallet.id, txid=ep.txid,
                direction=ep.direction, address=ep.address, value_sats=ep.value_sats))
        else:
            row.value_sats = ep.value_sats
    session.commit()


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

    persist_utxos(session, wallet, scan.utxos)
    persist_endpoints(session, wallet, scan.endpoints)

    mode = wallet.onchain_mode or "standalone"
    src = f"xpub:{wallet.id}"
    # Existing rows for this wallet-source, keyed by direction-ext, loaded once (was a SELECT per
    # tx); new rows are added with commit=False and flushed in a single commit at the end.
    existing_by_ext = {
        t.external_id: t for t in session.scalars(select(Transaction).where(
            Transaction.account_id == wallet.account_id, Transaction.source == src))
    }
    changed = False
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
        existing = existing_by_ext.get(ext)
        if existing is not None:
            # Re-sync: refresh the on-chain facts but PRESERVE kind — it may have been
            # reclassified (cross-wallet transfer) or edited by the user.
            existing.amount_sats = abs(oc.net_sats)
            existing.timestamp = oc.timestamp
            existing.address = oc.address
            existing.note = note
            changed = True
            res.skipped += 1
            continue
        existing_by_ext[ext] = tx_svc.add_transaction(
            session, account_id=wallet.account_id, wallet_id=wallet.id, kind=kind,
            timestamp=oc.timestamp, amount_sats=abs(oc.net_sats), txid=oc.txid,
            address=oc.address, counterparty="on-chain", source=src,
            external_id=ext, note=note, commit=False,
        )  # cache the new (unflushed) row so a repeat ext in this scan updates in place
        res.imported += 1
        changed = True
    if changed:
        session.commit()
    return res
