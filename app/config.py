# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Application settings — environment-driven, with local-first defaults."""
import os
from pathlib import Path

# Data dir: /data inside the container (StartOS/Umbrel mount), ./data locally.
_default_data = "/data" if os.path.isdir("/data") else str(Path(__file__).resolve().parent.parent / "data")
DATA_DIR = Path(os.environ.get("BTT_DATA_DIR", _default_data))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("BTT_DB_PATH", DATA_DIR / "btt.sqlite"))
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

# Privacy: outbound network is OFF unless the user explicitly opts in.
# Gates online price fetching (xpub sync is separately gated by a configured Electrum host).
ENABLE_NETWORK = os.environ.get("BTT_ENABLE_NETWORK", "0") == "1"

# Electrum server (electrs/Fulcrum) for xpub scanning. Empty => xpub sync disabled.
ELECTRUM_HOST = os.environ.get("BTT_ELECTRUM_HOST", "")
ELECTRUM_PORT = int(os.environ.get("BTT_ELECTRUM_PORT", "50001"))
ELECTRUM_USE_SSL = os.environ.get("BTT_ELECTRUM_SSL", "0") == "1"

# Tor SOCKS proxy — used automatically when the Electrum host is a .onion.
TOR_SOCKS_HOST = os.environ.get("BTT_TOR_SOCKS_HOST", "127.0.0.1")
TOR_SOCKS_PORT = int(os.environ.get("BTT_TOR_SOCKS_PORT", "9050"))

# Bitcoin network: "mainnet" or "testnet" (testnet/signet used for safe dev).
NETWORK = os.environ.get("BTT_NETWORK", "mainnet")

# Optional block-explorer base URL (mempool.space-compatible) for "view on explorer" links —
# e.g. your node's mempool on StartOS/Umbrel, a LAN instance, or a .onion. Empty = no links.
MEMPOOL_URL = os.environ.get("BTT_MEMPOOL_URL", "")

# Frontend assets: "local" (vendored — no external requests, the default) or "cdn" (dev
# convenience: Tailwind Play CDN + htmx from unpkg). Local is default so an ordinary launch
# never phones home; the vendored app/static/tailwind.css is built from input.css (see
# Dockerfile / build step). Opt into the CDN only for styling iteration: BTT_ASSETS=cdn.
ASSETS = os.environ.get("BTT_ASSETS", "local")

APP_NAME = "ArcaSats"
TAGLINE = "Track the Chain. Own the Future."
# Donation/support link — ArcaSats' BTCPay point-of-sale (hosted by DirigoBTC).
# Override with BTT_SUPPORT_URL (e.g. a fork pointing donations elsewhere).
SUPPORT_URL = os.environ.get(
    "BTT_SUPPORT_URL", "https://btcpay.dirigobtc.org/apps/3KEffN1tpkqFGeAry3bnxeKZ57TR/pos")
