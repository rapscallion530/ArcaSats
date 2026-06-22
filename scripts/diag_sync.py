"""Diagnostic: test the configured node connection, then sync every xpub wallet.

Prints ONLY connection status + import counts/timing/errors — never transaction
data, addresses, or the xpub. Uses the same DB + node config as the app.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Wallet, WalletType
from app.services import node_settings
from app.services import sync as sync_svc


def main():
    session = SessionLocal()
    cfg = node_settings.get_config(session)
    onion = cfg.electrum_host.endswith(".onion")
    print(f"node: {'(onion, via Tor)' if onion else cfg.electrum_host or '(unset)'} "
          f"port={cfg.electrum_port} tor={cfg.use_tor or onion} socks={cfg.tor_host}:{cfg.tor_port}")

    print("testing connection ...")
    tr = node_settings.test_connection(session, timeout=30)
    print(f"  connection ok={tr.ok} msg={tr.message!r} latency_ms={tr.latency_ms} height={tr.height}")
    if not tr.ok:
        print("  -> cannot sync until the connection works.")
        return

    wallets = session.scalars(select(Wallet).where(Wallet.wtype == WalletType.XPUB)).all()
    print(f"xpub wallets to sync: {len(wallets)}")
    for w in wallets:
        t0 = time.monotonic()
        res = sync_svc.sync_wallet(session, w.id)
        dt = time.monotonic() - t0
        print(f"  wallet#{w.id}: imported={res.imported} skipped={res.skipped} "
              f"errors={res.errors} elapsed={dt:.1f}s")


if __name__ == "__main__":
    main()
