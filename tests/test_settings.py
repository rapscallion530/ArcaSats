"""Node connection settings (Sparrow-style) + account rename."""
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account
from app.services import node_settings


def _aid(name: str) -> int:
    with SessionLocal() as s:
        return s.scalar(select(Account.id).where(Account.name == name))


def test_llm_models_gives_visible_feedback(client):
    # "List models" used to silently fill a hidden datalist (looked like nothing happened). The
    # route now returns a visible status message + refreshes the datalist out-of-band. A non-local
    # endpoint is deterministic (refused before any network), so assert that branch + the OOB wiring.
    r = client.post("/settings/llm/models",
                    data={"provider": "ollama", "base_url": "http://8.8.8.8:11434",
                          "datalist_id": "models-add"})
    assert r.status_code == 200
    assert "isn't on this machine" in r.text                       # visible reason, not blank
    assert 'id="models-add"' in r.text and "hx-swap-oob" in r.text  # datalist refreshed for the input


def test_get_config_seeds_and_saves(session):
    cfg = node_settings.get_config(session)
    assert cfg.id == 1
    cfg2 = node_settings.save_node(
        session, electrum_host="electrum.example.com", electrum_port=50002,
        use_ssl=True, use_tor=False, tor_host="127.0.0.1", tor_port=9050)
    assert cfg2.electrum_host == "electrum.example.com"
    assert node_settings.get_config(session).electrum_port == 50002


def test_build_client_clearnet_vs_onion(session):
    # clearnet, no tor
    node_settings.save_node(session, electrum_host="10.0.0.5", electrum_port=50001,
                              use_ssl=False, use_tor=False, tor_host="127.0.0.1", tor_port=9050)
    c = node_settings.build_client(session)
    assert c is not None and c.proxy_host is None

    # .onion -> tor auto-on, ssl off
    node_settings.save_node(session, electrum_host="abc.onion", electrum_port=50001,
                              use_ssl=True, use_tor=False, tor_host="127.0.0.1", tor_port=9150)
    c2 = node_settings.build_client(session)
    assert c2.proxy_host == "127.0.0.1" and c2.proxy_port == 9150 and c2.use_ssl is False


def test_explorer_is_private_classifies_hosts():
    """The block-explorer privacy heuristic: local/own instances are private (no warning);
    public explorers are flagged so the UI warns the txid would leak to a third party."""
    priv = node_settings.explorer_is_private
    # local / own instances -> private (no warning)
    assert priv("") is True                          # no URL configured -> no links rendered
    assert priv("http://localhost:3006") is True
    assert priv("http://127.0.0.1:3006") is True
    assert priv("http://192.168.1.50:3006") is True  # LAN
    assert priv("http://umbrel.local") is True       # mDNS
    assert priv("http://abcd1234efgh.onion") is True  # Tor
    # public explorers -> NOT private (warn)
    assert priv("https://mempool.space") is False
    assert priv("https://blockstream.info/tx") is False
    assert priv("https://8.8.8.8") is False          # public IP literal


def test_build_client_none_when_unset(session):
    node_settings.save_node(session, electrum_host="", electrum_port=50001,
                              use_ssl=False, use_tor=False, tor_host="127.0.0.1", tor_port=9050)
    assert node_settings.build_client(session) is None


def test_test_params_unreachable_fails_fast():
    r = node_settings.test_params(electrum_host="127.0.0.1", electrum_port=1, use_ssl=False,
                                  use_tor=False, tor_host="127.0.0.1", tor_port=9050, timeout=4)
    assert r.ok is False and "failed" in r.message.lower()


def test_settings_routes(client):
    assert client.get("/settings").status_code == 200
    client.post("/settings", data={"electrum_host": "node.local", "electrum_port": "50001"})
    assert "node.local" in client.get("/settings").text
    # test endpoint returns a status partial (unreachable -> failure text)
    r = client.post("/settings/test", data={"electrum_host": "127.0.0.1", "electrum_port": "1"})
    assert r.status_code == 200 and "node-status" in r.text


def test_save_node_and_mempool_are_independent(session):
    # The whole point of the feature: each form saves only its own fields.
    ns = node_settings
    ns.save_node(session, electrum_host="node.local", electrum_port=50001, use_ssl=False,
                 use_tor=False, tor_host="127.0.0.1", tor_port=9050)
    ns.save_mempool(session, mempool_url="http://mp.local:3006/", mempool_use_tor=True,
                    price_source="mempool")
    cfg = ns.get_config(session)
    assert cfg.electrum_host == "node.local"
    assert cfg.mempool_url == "http://mp.local:3006" and cfg.mempool_use_tor is True
    assert cfg.price_source == "mempool"
    # Re-saving the node must NOT clobber the mempool settings (and vice versa).
    ns.save_node(session, electrum_host="node2.local", electrum_port=50001, use_ssl=False,
                 use_tor=False, tor_host="127.0.0.1", tor_port=9050)
    cfg = ns.get_config(session)
    assert cfg.electrum_host == "node2.local"
    assert cfg.mempool_url == "http://mp.local:3006" and cfg.mempool_use_tor is True


def test_test_mempool_params_reports_states(session, monkeypatch):
    from app.services import http_fetch
    monkeypatch.setattr(http_fetch, "get_json", lambda *a, **k: {"prices": [{"USD": 90000}]})
    r = node_settings.test_mempool_params(mempool_url="http://m.local:3006")
    assert r.ok and "price data available" in r.message
    monkeypatch.setattr(http_fetch, "get_json", lambda *a, **k: {"prices": []})
    r = node_settings.test_mempool_params(mempool_url="http://m.local:3006")
    assert r.ok and "no price data" in r.message
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(http_fetch, "get_json", boom)
    r = node_settings.test_mempool_params(mempool_url="http://m.local:3006")
    assert not r.ok and "failed" in r.message.lower()


def test_test_mempool_params_routes_onion_via_tor(monkeypatch):
    from app.services import http_fetch
    seen = {}
    def fake(url, *, proxy_host=None, proxy_port=None, timeout=12.0):
        seen["proxy"] = proxy_host
        return {"prices": [{"USD": 1}]}
    monkeypatch.setattr(http_fetch, "get_json", fake)
    node_settings.test_mempool_params(mempool_url="http://abcd.onion:3006", tor_host="127.0.0.1", tor_port=9050)
    assert seen["proxy"] == "127.0.0.1"   # .onion -> routed through the SOCKS proxy
    node_settings.test_mempool_params(mempool_url="http://m.local:3006", tor_host="127.0.0.1", tor_port=9050)
    assert seen["proxy"] is None          # clearnet -> direct


def test_mempool_settings_routes(client):
    client.post("/settings/mempool", data={"mempool_url": "http://my.local:3006", "price_source": "mempool"})
    assert "my.local:3006" in client.get("/settings").text
    # unreachable host -> failure text, but the endpoint still renders the status partial (200)
    r = client.post("/settings/mempool/test", data={"mempool_url": "http://127.0.0.1:1"})
    assert r.status_code == 200 and "mempool-status" in r.text


def test_node_status_widget(client):
    r = client.get("/node/status")
    assert r.status_code == 200
    assert "accounts" in r.text  # stats line present
    assert "wallets" in r.text
    assert "refresh" in r.text


def test_transaction_edit_reclassify_sell_to_transfer(client):
    import re
    client.post("/accounts", data={"name": "TxReclass"})
    aid = _aid("TxReclass")
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "sell", "timestamp": "2025-03-01", "amount_btc": "0.1", "fiat_value": "6000"})
    page = client.get(f"/accounts/{aid}").text
    tid = re.search(rf"/accounts/{aid}/transactions/(\d+)/edit-form", page).group(1)
    # edit form renders a Type selector
    assert "Type" in client.get(f"/accounts/{aid}/transactions/{tid}/edit-form").text
    # reclassify sell -> transfer_out
    r = client.post(f"/accounts/{aid}/transactions/{tid}/edit",
                    data={"kind": "transfer_out", "timestamp": "2025-03-01", "amount_btc": "0.1", "fiat_value": ""})
    assert r.status_code == 200 and "Transfer out" in r.text


def test_fetch_prices_route(client):
    client.post("/accounts", data={"name": "PriceAcct"})
    aid = _aid("PriceAcct")
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "buy", "timestamp": "2025-01-01", "amount_btc": "0.01", "fiat_value": ""})
    r = client.post(f"/accounts/{aid}/prices/fetch")
    assert r.status_code == 200 and "Set USD prices" in r.text


def test_gift_statement_renders(client):
    import re
    client.post("/accounts", data={"name": "GiftAcct"})
    aid = _aid("GiftAcct")
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "transfer_out", "timestamp": "2025-02-01", "amount_btc": "0.05"})
    page = client.get(f"/accounts/{aid}").text
    tid = re.search(rf"/accounts/{aid}/transactions/(\d+)/gift-statement", page).group(1)
    r = client.get(f"/accounts/{aid}/transactions/{tid}/gift-statement?recipient=Bob")
    assert r.status_code == 200
    assert "Cost Basis Statement" in r.text and "0.05000000" in r.text and "Bob" in r.text


def test_carry_toggle_route(client):
    import re
    client.post("/accounts", data={"name": "CarryAcct"})
    aid = _aid("CarryAcct")
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "transfer_in", "timestamp": "2025-02-01", "amount_btc": "0.05"})
    tid = re.search(rf"/accounts/{aid}/transactions/(\d+)/edit-form", client.get(f"/accounts/{aid}").text).group(1)
    assert client.post(f"/accounts/{aid}/transactions/{tid}/carry-toggle").status_code == 200


def test_audit_route(client):
    import re
    client.post("/accounts", data={"name": "AuditAcct"})
    aid = _aid("AuditAcct")
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "buy", "timestamp": "2025-01-01", "amount_btc": "1.0", "fiat_value": "30000"})
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "sell", "timestamp": "2025-03-01", "amount_btc": "0.5", "fiat_value": "20000"})
    r = client.get(f"/accounts/{aid}/audit")
    assert r.status_code == 200
    assert "Open lots" in r.text and "Realized disposals" in r.text


def test_outbound_log_records_node_test(client):
    # A node connection test is an intentional outbound action -> logged.
    client.post("/settings/test", data={"electrum_host": "node.example", "electrum_port": "50001"})
    page = client.get("/settings").text
    assert "Outbound data log" in page and "node connection test" in page


def test_account_edit_all_fields(client):
    client.post("/accounts", data={"name": "EditOld"})
    aid = _aid("EditOld")
    assert "EditOld" in client.get(f"/accounts/{aid}/edit-form").text
    r = client.post(f"/accounts/{aid}/edit",
                    data={"name": "EditNew", "label_kind": "non-KYC", "note": "cold storage"})
    assert r.status_code == 200
    assert "EditNew" in r.text and "non-KYC" in r.text and "cold storage" in r.text
    assert "EditNew" in client.get("/accounts").text


def test_account_edit_duplicate_name_rejected(client):
    client.post("/accounts", data={"name": "DupAlpha"})
    client.post("/accounts", data={"name": "DupBeta"})
    aid = _aid("DupAlpha")
    r = client.post(f"/accounts/{aid}/edit", data={"name": "DupBeta"})
    assert "already exists" in r.text


def test_migration_0005_adds_mempool_use_tor(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    url = f"sqlite:///{(tmp_path / 'm.sqlite').as_posix()}"
    monkeypatch.setattr("app.config.DATABASE_URL", url)
    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    cols = {c["name"] for c in inspect(eng).get_columns("node_config")}
    eng.dispose()
    assert "mempool_use_tor" in cols
