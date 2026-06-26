# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Electrum node connection settings (Sparrow-style: server + port + Tor toggle).

The DB NodeConfig (singleton) is the source of truth, seeded from env on first use
so packaged deployments (StartOS/Umbrel) can preset it. `.onion` hosts route over
Tor automatically; a Tor toggle also lets clearnet hosts go over Tor for privacy.
"""
from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app import config
from app.models import NodeConfig
from app.services.electrum import ElectrumClient


def explorer_is_private(url: str) -> bool:
    """UX heuristic (NOT an egress gate): is the configured block-explorer URL a local/own
    instance, so clicking its tx link doesn't leak the txid (and your IP) to a third party?

    True for: an empty URL (no links rendered), localhost, a loopback/private/link-local IP,
    a `.onion` (Tor), or a `.local` (mDNS) host. A public host (e.g. mempool.space) returns
    False so the UI can warn. Deliberately DNS-free — rendering a settings page must not trigger
    a network lookup (contrast `llm.is_local`, which resolves the host and excludes `.onion`)."""
    host = (urlparse(url).hostname or "").lower() if url else ""
    if not host or host == "localhost" or host.endswith((".onion", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # an ordinary public hostname — treat as public (warn)
    return ip.is_loopback or ip.is_private or ip.is_link_local


def get_config(session: Session) -> NodeConfig:
    cfg = session.get(NodeConfig, 1)
    if cfg is None:
        host = config.ELECTRUM_HOST.strip()
        cfg = NodeConfig(
            id=1,
            electrum_host=host,
            electrum_port=config.ELECTRUM_PORT,
            use_ssl=config.ELECTRUM_USE_SSL,
            use_tor=host.endswith(".onion"),
            tor_host=config.TOR_SOCKS_HOST,
            tor_port=config.TOR_SOCKS_PORT,
            mempool_url=config.MEMPOOL_URL,
        )
        session.add(cfg)
        session.commit()
        session.refresh(cfg)
    return cfg


def save_node(session: Session, *, electrum_host: str, electrum_port: int, use_ssl: bool,
              use_tor: bool, tor_host: str, tor_port: int) -> NodeConfig:
    """Save the Electrum NODE connection (+ the shared Tor SOCKS proxy). Leaves the mempool /
    price-source settings untouched so the two can be configured independently."""
    cfg = get_config(session)
    cfg.electrum_host = electrum_host.strip()
    cfg.electrum_port = electrum_port
    cfg.use_ssl = use_ssl
    cfg.use_tor = use_tor
    cfg.tor_host = (tor_host or "127.0.0.1").strip()
    cfg.tor_port = tor_port
    session.commit()
    session.refresh(cfg)
    return cfg


def save_mempool(session: Session, *, mempool_url: str, mempool_use_tor: bool = False,
                 price_source: str | None = None) -> NodeConfig:
    """Save the MEMPOOL connection (block-explorer base + price API) and the USD price source.
    Independent of the node; the Tor toggle reuses the SOCKS proxy saved with the node."""
    cfg = get_config(session)
    cfg.mempool_url = (mempool_url or "").strip().rstrip("/")
    cfg.mempool_use_tor = bool(mempool_use_tor)
    from app.services import pricing  # lazy: avoid import cycle; registry is the source of truth
    if price_source in pricing.PRICE_SOURCES:
        cfg.price_source = price_source
    session.commit()
    session.refresh(cfg)
    return cfg


def build_client(session: Session, timeout: float = 30.0) -> ElectrumClient | None:
    cfg = get_config(session)
    host = cfg.electrum_host.strip()
    if not host:
        return None
    is_onion = host.endswith(".onion")
    via_tor = cfg.use_tor or is_onion
    return ElectrumClient(
        host=host,
        port=cfg.electrum_port,
        use_ssl=cfg.use_ssl and not via_tor,  # Tor already encrypts the hop
        timeout=timeout,
        proxy_host=cfg.tor_host if via_tor else None,
        proxy_port=cfg.tor_port if via_tor else None,
    )


@dataclass
class TestResult:
    ok: bool
    message: str
    height: int | None = None
    latency_ms: int | None = None


def _test_client(client: ElectrumClient | None) -> TestResult:
    if client is None:
        return TestResult(False, "No server configured.")
    start = time.monotonic()
    try:
        client.connect()
        height = client.block_height()
    except Exception as exc:  # noqa: BLE001
        return TestResult(False, f"Connection failed: {exc}")
    finally:
        client.close()
    return TestResult(True, "Connected", height=height, latency_ms=int((time.monotonic() - start) * 1000))


def test_connection(session: Session, timeout: float = 12.0) -> TestResult:
    """Test the saved config."""
    return _test_client(build_client(session, timeout=timeout))


def test_params(*, electrum_host: str, electrum_port: int, use_ssl: bool, use_tor: bool,
                tor_host: str, tor_port: int, timeout: float = 12.0) -> TestResult:
    """Test explicit NODE values (lets the UI test before saving), Sparrow-style."""
    host = (electrum_host or "").strip()
    if not host:
        return TestResult(False, "Enter a server address first.")
    via_tor = use_tor or host.endswith(".onion")
    client = ElectrumClient(
        host=host, port=electrum_port, use_ssl=use_ssl and not via_tor, timeout=timeout,
        proxy_host=(tor_host or "127.0.0.1") if via_tor else None,
        proxy_port=tor_port if via_tor else None,
    )
    return _test_client(client)


def test_mempool_params(*, mempool_url: str, mempool_use_tor: bool = False,
                        tor_host: str = "127.0.0.1", tor_port: int = 9050,
                        timeout: float = 12.0) -> TestResult:
    """Test the MEMPOOL connection by probing its historical-price API — the app's actual mempool
    use. Routes over the Tor SOCKS proxy for a .onion host (or when opted in). Reachable-with-no-
    price is reported as OK with guidance (the instance just needs price indexing enabled)."""
    url = (mempool_url or "").strip().rstrip("/")
    if not url:
        return TestResult(False, "Enter your mempool URL first.")
    from app.services import http_fetch
    host = urlparse(url).hostname or ""
    via = http_fetch.via_tor(host, mempool_use_tor)
    probe = f"{url}/api/v1/historical-price?currency=USD&timestamp={int(time.time()) - 86400}"
    start = time.monotonic()
    try:
        body = http_fetch.get_json(
            probe, proxy_host=(tor_host or "127.0.0.1") if via else None,
            proxy_port=tor_port if via else None, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return TestResult(False, f"Connection failed: {exc}")
    latency = int((time.monotonic() - start) * 1000)
    prices = (body or {}).get("prices") or []
    if prices and prices[0].get("USD"):
        return TestResult(True, "Connected — price data available", latency_ms=latency)
    return TestResult(True, "Connected, but no price data returned — enable price indexing on your mempool.",
                      latency_ms=latency)
