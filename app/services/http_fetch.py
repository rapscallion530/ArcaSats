# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Minimal HTTP(S) GET -> JSON, optionally via a Tor SOCKS5 proxy (for `.onion` hosts).

Clearnet uses urllib; the Tor path reuses `electrum._socks5_connect` over a raw socket so we add
no SOCKS dependency. Used for the user's OWN mempool instance (price API + the connection test) —
the third-party price fetchers stay on the clearnet urllib path in pricing.py.
"""
from __future__ import annotations

import json
import socket
import ssl
import urllib.request
from urllib.parse import urlparse

from app.services.electrum import _is_lan_host, _socks5_connect

_HEADERS = {"User-Agent": "bitcoin-tax-tracker", "Accept": "application/json"}
_MAX_BYTES = 8 * 1024 * 1024  # cap a single response so a hostile/buggy server can't OOM us


def via_tor(host: str, flag: bool) -> bool:
    """Should this host be reached over Tor? Explicit opt-in, or any `.onion` (which can ONLY be
    reached via the SOCKS proxy) — same rule the Electrum client uses."""
    return bool(flag) or (host or "").endswith(".onion")


def get_json(url: str, *, proxy_host: str | None = None, proxy_port: int | None = None,
             timeout: float = 12.0):
    """GET `url` and parse JSON. Routes through the SOCKS5 proxy when `proxy_host` is set."""
    if not proxy_host:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    return _get_json_socks(url, proxy_host, proxy_port, timeout)


def _dechunk(body: bytes) -> bytes:
    """Decode HTTP/1.1 chunked transfer-encoding (servers may chunk even with Connection: close)."""
    out, rest = b"", body
    while rest:
        size_line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            break
        try:
            size = int(size_line.strip().split(b";", 1)[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        out += rest[:size]
        rest = rest[size + 2:]  # skip the chunk's trailing CRLF
    return out


def _get_json_socks(url: str, proxy_host: str, proxy_port: int, timeout: float):
    u = urlparse(url)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    raw = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    raw.settimeout(timeout)
    try:
        _socks5_connect(raw, host, port)
        sock = raw
        if u.scheme == "https":
            ctx = ssl.create_default_context()
            if _is_lan_host(host):  # self-signed is normal for .onion/.local/LAN
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw, server_hostname=host)
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
               "User-Agent: bitcoin-tax-tracker\r\nAccept: application/json\r\n"
               "Connection: close\r\n\r\n")
        sock.sendall(req.encode())
        data = b""
        while len(data) <= _MAX_BYTES:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        try:
            raw.close()
        except OSError:
            pass
    head, _, body = data.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode("latin1", "replace").split(" ")
    code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
    if code != 200:
        raise OSError(f"HTTP {code or '?'} from {host}")
    if b"transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    return json.loads(body.decode())
