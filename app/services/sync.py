# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Wire xpub wallets to a configured Electrum server and sync on demand.

Network only happens here, when the user triggers a sync and a host is configured.
.onion hosts are routed through the Tor SOCKS proxy automatically.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Wallet, WalletType
from app.services import node_settings
from app.services.importers.csv_import import ImportResult
from app.services.importers.xpub import import_xpub


def sync_wallet(session: Session, wallet_id: int) -> ImportResult:
    res = ImportResult()
    wallet = session.get(Wallet, wallet_id)
    if wallet is None or wallet.wtype != WalletType.XPUB or not wallet.xpub:
        res.errors.append("not an xpub wallet")
        return res

    client = node_settings.build_client(session)
    if client is None:
        res.errors.append("No Electrum server configured. Set one in Settings → Node.")
        return res

    from app.services import outbound
    outbound.record(client.host, "xpub sync (Electrum%s)" % (" over Tor" if client.proxy_host else ""))
    try:
        client.connect()
    except Exception as exc:  # noqa: BLE001
        res.errors.append(f"could not connect to Electrum server: {exc}")
        return res
    try:
        return import_xpub(session, wallet=wallet, client=client)
    finally:
        client.close()
