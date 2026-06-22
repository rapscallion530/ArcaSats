# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Minimal Electrum protocol client (electrs / Fulcrum).

Speaks the line-delimited JSON-RPC Electrum protocol. Used to look up address
(scripthash) history and fetch verbose transactions for xpub scanning.

Network calls only happen when the user configures an Electrum host AND triggers a
sync — never automatically. Tests use a mock implementing the same interface.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import ssl
from typing import Protocol, runtime_checkable

_MAX_RESPONSE = 32 * 1024 * 1024  # cap a single JSON-RPC response so a buggy/hostile server
#                                   can't grow the read buffer until OOM


@runtime_checkable
class ElectrumLike(Protocol):
    def get_history(self, scripthash: str) -> list[dict]: ...
    def get_transaction(self, txid: str, verbose: bool = True) -> dict: ...


class ElectrumError(RuntimeError):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read EXACTLY n bytes. TCP is a stream — a single recv() can return fewer bytes than
    requested, so the original fixed-size recv() calls could leave leftover bytes that
    corrupt the next read (an intermittent failure mode, especially over Tor)."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ElectrumError("connection closed during SOCKS5 handshake")
        buf += chunk
    return buf


def _socks5_connect(sock: socket.socket, dest_host: str, dest_port: int) -> None:
    """Minimal SOCKS5 CONNECT (no auth) — for reaching .onion via Tor."""
    sock.sendall(b"\x05\x01\x00")
    if _recv_exact(sock, 2) != b"\x05\x00":
        raise ElectrumError("SOCKS5 handshake failed (proxy running?)")
    host_b = dest_host.encode()
    req = b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + dest_port.to_bytes(2, "big")
    sock.sendall(req)
    resp = _recv_exact(sock, 4)
    if resp[1] != 0x00:
        raise ElectrumError(f"SOCKS5 connect failed (code {resp[1]}) — is the .onion reachable / Tor up?")
    # drain the bound address (length depends on the address type byte)
    atyp = resp[3]
    if atyp == 0x01:      # IPv4 + port
        _recv_exact(sock, 4 + 2)
    elif atyp == 0x03:    # domain name + port
        ln = _recv_exact(sock, 1)
        _recv_exact(sock, ln[0] + 2)
    elif atyp == 0x04:    # IPv6 + port
        _recv_exact(sock, 16 + 2)


def _is_lan_host(host: str) -> bool:
    """LAN/loopback IP, or a .local/.onion name — where a self-signed Electrum cert is normal
    and strict TLS validation would only get in the way. Public clearnet hosts are NOT LAN
    and must have their certs verified."""
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return host.endswith(".local") or host.endswith(".onion")


class ElectrumClient:
    """Blocking, single-connection Electrum client."""

    def __init__(self, host: str, port: int = 50001, use_ssl: bool = False, timeout: float = 30.0,
                 proxy_host: str | None = None, proxy_port: int | None = None):
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.timeout = timeout
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self._sock: socket.socket | None = None
        self._buf = b""
        self._id = 0

    # -- connection --
    def connect(self) -> None:
        if self.proxy_host:
            raw = socket.create_connection((self.proxy_host, self.proxy_port), timeout=self.timeout)
            raw.settimeout(self.timeout)
            _socks5_connect(raw, self.host, self.port)
        else:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.use_ssl:
            ctx = ssl.create_default_context()
            # Verify certs for public clearnet hosts (a MITM there would see every scripthash
            # we query, i.e. your wallet's addresses). Only drop verification for LAN/loopback
            # or .local/.onion hosts, where self-signed electrs certs are the norm.
            if _is_lan_host(self.host):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        self._sock = raw
        self._call("server.version", ["bitcoin-tax-tracker", "1.4"])

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- rpc --
    def _call(self, method: str, params: list):
        if self._sock is None:
            raise ElectrumError("not connected")
        self._id += 1
        req = {"id": self._id, "method": method, "params": params}
        self._sock.sendall((json.dumps(req) + "\n").encode())
        while b"\n" not in self._buf:
            chunk = self._sock.recv(8192)
            if not chunk:
                raise ElectrumError("connection closed")
            self._buf += chunk
            if len(self._buf) > _MAX_RESPONSE:
                raise ElectrumError("electrum response exceeded size limit")
        line, self._buf = self._buf.split(b"\n", 1)
        resp = json.loads(line.decode())
        if resp.get("error"):
            raise ElectrumError(str(resp["error"]))
        return resp.get("result")

    def get_history(self, scripthash: str) -> list[dict]:
        return self._call("blockchain.scripthash.get_history", [scripthash]) or []

    def get_transaction(self, txid: str, verbose: bool = True) -> dict:
        return self._call("blockchain.transaction.get", [txid, verbose])

    def block_height(self) -> int:
        """Current chain tip height (for connection tests / status)."""
        res = self._call("blockchain.headers.subscribe", [])
        return int(res.get("height", 0)) if isinstance(res, dict) else 0
