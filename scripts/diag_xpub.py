"""Diagnose an xpub script-type mismatch: for the loaded wallet, check on-chain
history counts for each address encoding (p2pkh / p2sh-p2wpkh / p2wpkh).

Prints ONLY the key prefix + per-type tx COUNTS — never the xpub or any address.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Wallet, WalletType
from app.services import node_settings
from app.services.bip32 import _ckd_pub, _deserialize, key_kind, pubkey_to_address
from app.services.script import scripthash

TYPES = ["p2pkh", "p2sh-p2wpkh", "p2wpkh"]


def main():
    session = SessionLocal()
    wallet = session.scalars(select(Wallet).where(Wallet.wtype == WalletType.XPUB)).first()
    if wallet is None or not wallet.xpub:
        print("no xpub wallet loaded")
        return
    xpub = wallet.xpub.strip()
    declared, network = key_kind(xpub)
    print(f"key prefix: {xpub[:4]}   declared script type: {declared}   network: {network}   gap: {wallet.gap_limit}")

    client = node_settings.build_client(session, timeout=40)
    client.connect()
    print("connected. checking first receive addresses in each encoding (counts only):")
    chaincode, key = _deserialize(xpub)
    ck, cc = _ckd_pub(key, chaincode, 0)  # receive branch
    for i in range(3):
        leaf, _ = _ckd_pub(ck, cc, i)
        row = []
        for st in TYPES:
            addr = pubkey_to_address(leaf, st, network)
            n = len(client.get_history(scripthash(addr)))
            row.append(f"{st}={n}")
        print(f"  recv[{i}]  " + "  ".join(row))
    client.close()


if __name__ == "__main__":
    main()
