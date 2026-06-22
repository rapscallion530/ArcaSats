"""Local LLM connection management + chat client (no real network)."""
import datetime as dt
from decimal import Decimal

from app.models import TxKind
from app.services import accounts as acc
from app.services import assistant as assistant_svc
from app.services import llm
from app.services import transactions as txs


def test_is_local_classification():
    # IP literals classified directly (no DNS).
    assert llm.is_local("http://127.0.0.1:11434")
    assert llm.is_local("http://192.168.18.10:11434")
    assert llm.is_local("http://[::1]:11434")                 # IPv6 loopback
    assert not llm.is_local("http://8.8.8.8:11434")           # public v4
    assert not llm.is_local("http://[::ffff:8.8.8.8]:11434")  # IPv4-mapped public v6
    # localhost resolves to loopback -> local.
    assert llm.is_local("http://localhost:1234/v1")
    # Hardened gate: a name is only local if it RESOLVES to local addresses. A public host,
    # or a .local/.internal name that doesn't resolve (or resolves public), is NOT trusted.
    assert not llm.is_local("https://api.openai.com/v1")
    assert not llm.is_local("http://does-not-exist.internal:11434")


def test_connection_crud_and_default(session):
    a = llm.add_connection(session, name="Qwen", provider="ollama",
                           base_url="http://127.0.0.1:11434/", model="qwen2.5:7b")
    assert a.is_default is True                     # first connection becomes default
    assert a.base_url == "http://127.0.0.1:11434"   # trailing slash trimmed
    b = llm.add_connection(session, name="Dolphin", provider="ollama",
                           base_url="http://127.0.0.1:11434", model="dolphin3")
    assert b.is_default is False
    llm.set_default(session, b.id)
    assert llm.get_default(session).id == b.id
    # Deleting the default promotes the remaining one.
    llm.delete_connection(session, b.id)
    assert llm.get_default(session).id == a.id


def test_endpoints_per_provider(session):
    o = llm.add_connection(session, name="o", provider="ollama", base_url="http://127.0.0.1:11434", model="m")
    chat_url, models_url = llm._endpoints(o)
    assert chat_url.endswith("/api/chat") and models_url.endswith("/api/tags")
    p = llm.add_connection(session, name="p", provider="openai", base_url="http://127.0.0.1:1234", model="m")
    chat_url, models_url = llm._endpoints(p)
    assert chat_url.endswith("/v1/chat/completions") and models_url.endswith("/v1/models")


def test_chat_blocks_nonlocal(session):
    c = llm.add_connection(session, name="remote", provider="openai",
                           base_url="https://api.example.com/v1", model="gpt")
    res = llm.chat(c, [{"role": "user", "content": "hi"}])
    assert res.ok is False and "machine" in res.error.lower()


def test_assistant_is_loopback_only_by_default(session):
    # Loopback is this machine; a LAN address is not, and the assistant blocks it by default.
    assert llm.is_loopback("http://127.0.0.1:11434")
    assert not llm.is_loopback("http://192.168.18.10:11434")
    assert llm.assistant_endpoint_allowed("http://127.0.0.1:11434")
    assert not llm.assistant_endpoint_allowed("http://192.168.18.10:11434")  # LAN blocked unless opted in
    lan = llm.add_connection(session, name="lan", provider="ollama",
                             base_url="http://192.168.18.10:11434", model="qwen3:8b")
    assert llm.chat(lan, [{"role": "user", "content": "hi"}]).ok is False


def test_chat_parses_ollama_and_openai(session, monkeypatch):
    o = llm.add_connection(session, name="o", provider="ollama", base_url="http://127.0.0.1:11434", model="m")
    monkeypatch.setattr(llm, "_post_json", lambda *a, **k: {"message": {"content": "hello from ollama"}})
    res = llm.chat(o, [{"role": "user", "content": "hi"}])
    assert res.ok and res.text == "hello from ollama"

    p = llm.add_connection(session, name="p", provider="openai", base_url="http://127.0.0.1:1234", model="m")
    monkeypatch.setattr(llm, "_post_json",
                        lambda *a, **k: {"choices": [{"message": {"content": "hi from openai"}}]})
    res = llm.chat(p, [{"role": "user", "content": "hi"}])
    assert res.ok and res.text == "hi from openai"


def test_chat_requires_model(session):
    c = llm.add_connection(session, name="c", provider="ollama", base_url="http://127.0.0.1:11434", model="")
    res = llm.chat(c, [{"role": "user", "content": "hi"}])
    assert res.ok is False and "model" in res.error.lower()


def test_assistant_snapshot_grounded_in_engine(session):
    a = acc.create_account(session, name="Personal")
    # A buy then a sell -> a realized disposal the snapshot must surface.
    txs.add_transaction(session, account_id=a.id, kind=TxKind.BUY,
                        timestamp=dt.datetime(2023, 1, 1), amount_sats=txs.btc_to_sats("0.10"),
                        fiat_value=Decimal("3000.00"))
    txs.add_transaction(session, account_id=a.id, kind=TxKind.SELL,
                        timestamp=dt.datetime(2024, 6, 1), amount_sats=txs.btc_to_sats("0.05"),
                        fiat_value=Decimal("3000.00"))
    snap = assistant_svc.build_snapshot(session)
    assert snap["accounts"][0]["name"] == "Personal"
    assert snap["years_with_disposals"] == [2024]
    assert any(d["account"] == "Personal" and d["kind"] == "sell" for d in snap["disposals"])
    # 0.05 BTC sold for 3000, basis 1500 -> 1500 long-ish gain; just assert it's surfaced & positive.
    assert snap["totals"]["realized_long_usd"] + snap["totals"]["realized_short_usd"] > 0


def test_assistant_ask_builds_messages_and_calls_chat(session, monkeypatch):
    a = acc.create_account(session, name="Cold")
    c = llm.add_connection(session, name="o", provider="ollama", base_url="http://127.0.0.1:11434", model="m")
    captured = {}

    def fake_chat(conn, messages, timeout=120.0):
        captured["messages"] = messages
        return llm.ChatResult(True, text="ok")

    monkeypatch.setattr(llm, "chat", fake_chat)
    res = assistant_svc.ask(session, c, "How much do I hold?")
    assert res.ok
    assert captured["messages"][0]["role"] == "system"
    assert "SNAPSHOT" in captured["messages"][1]["content"]
