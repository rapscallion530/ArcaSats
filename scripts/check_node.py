"""Diagnostic: validate Electrum (electrs/Fulcrum) connectivity end-to-end.

Usage:
    python scripts/check_node.py [host] [port]

Connects, runs the server.version handshake, derives a few addresses from the
PUBLIC BIP84 test xpub (no personal data), queries history, and probes whether
the server supports verbose `blockchain.transaction.get` (electrs vs Fulcrum).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.bip32 import derive_addresses
from app.services.electrum import ElectrumClient
from app.services.script import scripthash

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"  # pass your electrs host as arg 1
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 50001
# .onion hosts route through the Tor SOCKS proxy (Tor Browser=9150, tor service=9050).
IS_ONION = HOST.endswith(".onion")
PROXY_HOST = "127.0.0.1" if IS_ONION else None
PROXY_PORT = int(os.environ.get("BTT_TOR_SOCKS_PORT", "9150")) if IS_ONION else None
# BIP84 official test vector account zpub (mnemonic "abandon ... about") — public.
ZPUB = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868Rv"
        "UUkgDKf31mGDtKsAYz2oz2AGutZYs")


def main():
    via = f" via Tor ({PROXY_HOST}:{PROXY_PORT})" if IS_ONION else ""
    print(f"connecting to {HOST}:{PORT}{via} ...")
    c = ElectrumClient(HOST, PORT, use_ssl=False, timeout=45,
                       proxy_host=PROXY_HOST, proxy_port=PROXY_PORT)
    c.connect()
    print("OK: connected + server.version handshake succeeded")

    found = None
    for change in (0, 1):
        for idx, addr in derive_addresses(ZPUB, change=change, count=4):
            hist = c.get_history(scripthash(addr))
            print(f"  {'recv' if change == 0 else 'chng'}[{idx}] {addr} -> {len(hist)} tx")
            if hist and found is None:
                found = (addr, hist[0]["tx_hash"])

    if found is None:
        print("get_history works (test addresses have no history). Could not test verbose tx.")
        c.close()
        return

    addr, txid = found
    print(f"\nprobing verbose get_transaction on {txid} ...")
    try:
        tx = c.get_transaction(txid, verbose=True)
        if isinstance(tx, dict):
            spk = (tx.get("vout") or [{}])[0].get("scriptPubKey", {})
            print("VERBOSE: SUPPORTED")
            print("  vout[0] has address field:", ("address" in spk) or ("addresses" in spk))
            print("  has blocktime/time:", ("blocktime" in tx) or ("time" in tx))
        else:
            print("VERBOSE: returned non-dict (raw?) ->", type(tx))
    except Exception as exc:  # noqa: BLE001
        print("VERBOSE: NOT supported ->", repr(str(exc)))
        raw = c.get_transaction(txid, verbose=False)
        print("  raw hex works, length:", len(raw) if isinstance(raw, str) else raw)
        print("  => scanner needs a raw-tx parser fallback for this server.")
    c.close()


if __name__ == "__main__":
    main()
