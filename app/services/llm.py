# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Local LLM connections + a thin chat client.

Supports two endpoint styles, both commonly served locally:
  - "ollama"  -> POST {base}/api/chat, models via {base}/api/tags
  - "openai"  -> POST {base}/v1/chat/completions, models via {base}/v1/models
                 (LM Studio, llama.cpp --api, vLLM, etc.)

Privacy: the assistant feeds the model your real coin data, so by default we refuse to
talk to anything that isn't loopback/LAN. A connection must explicitly set allow_remote
to use a non-local endpoint. Every call is recorded in the Outbound Data Log (host + model
only — never the prompt contents).
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LLMConnection
from app.services import outbound

PROVIDERS = ("ollama", "openai")


# --- privacy / locality ------------------------------------------------------
def host_of(base_url: str) -> str:
    try:
        return (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return ""


def _unmap(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    # Judge an IPv4-mapped IPv6 literal (e.g. ::ffff:8.8.8.8) by its real v4 address.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _all_addrs_match(base_url: str, pred) -> bool:
    """True iff the endpoint's host is, or RESOLVES ENTIRELY to, addresses satisfying pred.

    Hostnames are resolved and EVERY resolved address must satisfy pred — so a name like
    `db.internal` that resolves to a public IP does NOT pass (the old name-suffix trust was a
    DNS-rebinding-style exfiltration hole). Unresolvable names fail closed."""
    host = host_of(base_url)
    if not host:
        return False
    try:  # IP literal — classify directly, no DNS.
        return pred(_unmap(ipaddress.ip_address(host)))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    addrs = {info[4][0].split("%")[0] for info in infos}  # strip IPv6 zone id
    if not addrs:
        return False
    for a in addrs:
        try:
            if not pred(_unmap(ipaddress.ip_address(a))):
                return False
        except ValueError:
            return False
    return True


def is_local(base_url: str) -> bool:
    """Loopback, private LAN, or link-local (the broad 'on your network' sense)."""
    return _all_addrs_match(base_url, lambda ip: ip.is_loopback or ip.is_private or ip.is_link_local)


def is_loopback(base_url: str) -> bool:
    """Strictly THIS machine (127.0.0.0/8, ::1) — excludes other LAN hosts."""
    return _all_addrs_match(base_url, lambda ip: ip.is_loopback)


def assistant_endpoint_allowed(base_url: str) -> bool:
    """The assistant sends your full portfolio snapshot, so by default it talks ONLY to a model
    on THIS machine (loopback) — honoring "data never leaves this machine". Set
    BTT_ASSISTANT_ALLOW_LAN=1 to also allow a model elsewhere on your private LAN."""
    import os
    if os.environ.get("BTT_ASSISTANT_ALLOW_LAN", "0") == "1":
        return is_local(base_url)
    return is_loopback(base_url)


# --- connection CRUD ---------------------------------------------------------
def list_connections(session: Session) -> list[LLMConnection]:
    return list(session.scalars(select(LLMConnection).order_by(LLMConnection.id)))


def get_connection(session: Session, conn_id: int) -> LLMConnection | None:
    return session.get(LLMConnection, conn_id)


def get_default(session: Session) -> LLMConnection | None:
    conn = session.scalar(select(LLMConnection).where(LLMConnection.is_default.is_(True)))
    return conn or session.scalar(select(LLMConnection).order_by(LLMConnection.id))


def add_connection(session: Session, *, name: str, provider: str, base_url: str, model: str,
                   api_key: str = "", allow_remote: bool = False) -> LLMConnection:
    provider = provider if provider in PROVIDERS else "ollama"
    first = session.scalar(select(LLMConnection)) is None
    conn = LLMConnection(
        name=name.strip() or "Local model", provider=provider, base_url=base_url.strip().rstrip("/"),
        model=model.strip(), api_key=api_key.strip(), allow_remote=allow_remote, is_default=first,
    )
    session.add(conn)
    session.commit()
    session.refresh(conn)
    return conn


def update_connection(session: Session, conn_id: int, **fields) -> LLMConnection | None:
    conn = session.get(LLMConnection, conn_id)
    if conn is None:
        return None
    for key in ("name", "provider", "base_url", "model", "api_key"):
        if key in fields and fields[key] is not None:
            val = str(fields[key]).strip()
            setattr(conn, key, val.rstrip("/") if key == "base_url" else val)
    if conn.provider not in PROVIDERS:
        conn.provider = "ollama"
    if "allow_remote" in fields:
        conn.allow_remote = bool(fields["allow_remote"])
    session.commit()
    session.refresh(conn)
    return conn


def set_default(session: Session, conn_id: int) -> None:
    for conn in list_connections(session):
        conn.is_default = (conn.id == conn_id)
    session.commit()


def delete_connection(session: Session, conn_id: int) -> bool:
    conn = session.get(LLMConnection, conn_id)
    if conn is None:
        return False
    was_default = conn.is_default
    session.delete(conn)
    session.commit()
    if was_default:  # promote another to default
        nxt = session.scalar(select(LLMConnection).order_by(LLMConnection.id))
        if nxt:
            nxt.is_default = True
            session.commit()
    return True


# --- chat client -------------------------------------------------------------
@dataclass
class ChatResult:
    ok: bool
    text: str = ""
    error: str = ""
    latency_ms: int | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow HTTP redirects. Without this, a local endpoint could 3xx-redirect the
    request (carrying your data) to an arbitrary host AFTER the is_local() check — defeating it."""
    def redirect_request(self, *_a, **_k):  # returning None makes urllib raise instead of follow
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post_json(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310  (no-redirect opener)
        return json.loads(resp.read().decode())


def _endpoints(conn: LLMConnection) -> tuple[str, str]:
    """(chat_url, models_url) for the connection's provider."""
    base = conn.base_url.rstrip("/")
    if conn.provider == "openai":
        # Append the /v1 prefix unless the base path already ends in it (a loose substring
        # test would mishandle paths like /v1beta or /api/v1x).
        root = base if base.endswith("/v1") else base + "/v1"
        return root + "/chat/completions", root + "/models"
    return base + "/api/chat", base + "/api/tags"


def chat(conn: LLMConnection, messages: list[dict], timeout: float = 120.0) -> ChatResult:
    """Send a chat completion. Refuses non-local endpoints unless allow_remote is set."""
    if not conn.model:
        return ChatResult(False, error="No model selected for this connection.")
    # Hard local-only: the assistant sends your real coin data, so by default it ONLY talks to
    # a model on THIS machine (loopback). Re-checked here at call time; redirects blocked below.
    if not assistant_endpoint_allowed(conn.base_url):
        return ChatResult(False, error=(
            "Endpoint isn't on this machine. ArcaSats only sends your data to a model at a "
            "loopback address (e.g. 127.0.0.1). To allow a model elsewhere on your LAN, set "
            "BTT_ASSISTANT_ALLOW_LAN=1."))

    chat_url, _ = _endpoints(conn)
    outbound.record(host_of(conn.base_url) or conn.base_url, "local AI assistant", conn.model[:60])
    start = time.monotonic()
    try:
        if conn.provider == "openai":
            payload = {"model": conn.model, "messages": messages, "stream": False, "temperature": 0.2}
            body = _post_json(chat_url, payload, conn.api_key, timeout)
            text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        else:
            payload = {"model": conn.model, "messages": messages, "stream": False,
                       "options": {"temperature": 0.2}}
            body = _post_json(chat_url, payload, conn.api_key, timeout)
            text = body.get("message", {}).get("content", "")
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        detail = ""
        try:
            detail = exc.read().decode()[:200]
        except Exception:  # noqa: BLE001
            pass
        return ChatResult(False, error=f"HTTP {exc.code} from model server. {detail}".strip())
    except urllib.error.URLError as exc:
        return ChatResult(False, error=f"Could not reach the model server: {exc.reason}. Is it running?")
    except Exception as exc:  # noqa: BLE001
        return ChatResult(False, error=f"Unexpected error: {exc}")

    latency = int((time.monotonic() - start) * 1000)
    if not (text or "").strip():
        return ChatResult(False, error="The model returned an empty response.", latency_ms=latency)
    return ChatResult(True, text=text.strip(), latency_ms=latency)


def list_models(conn: LLMConnection, timeout: float = 8.0) -> list[str]:
    """Best-effort model list from the endpoint (for the UI dropdown). Empty on failure."""
    if not assistant_endpoint_allowed(conn.base_url):
        return []
    _, models_url = _endpoints(conn)
    try:
        req = urllib.request.Request(models_url, headers={"Accept": "application/json"})
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310  (no-redirect opener)
            body = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001
        return []
    if conn.provider == "openai":
        return [d.get("id", "") for d in body.get("data", []) if d.get("id")]
    return [m.get("name", "") for m in body.get("models", []) if m.get("name")]


def test_connection(conn: LLMConnection, timeout: float = 30.0) -> ChatResult:
    """A tiny round-trip to confirm the model answers."""
    return chat(conn, [
        {"role": "user", "content": "Reply with exactly: ArcaSats connection OK"},
    ], timeout=timeout)
