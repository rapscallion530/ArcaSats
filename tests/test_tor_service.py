"""Bundled/managed Tor: binary resolution, torrc safety, version/update logic, and the
node_settings proxy override. No real Tor process or network is started here."""
from app.services import node_settings
from app.services import tor_service


def test_resolve_binary_prefers_env(monkeypatch, tmp_path):
    fake = tmp_path / "tor.exe"
    fake.write_text("")
    monkeypatch.setenv("BTT_TOR_BINARY", str(fake))
    assert tor_service.resolve_binary() == str(fake)


def test_resolve_binary_uses_cached_download(monkeypatch, tmp_path):
    monkeypatch.delenv("BTT_TOR_BINARY", raising=False)
    monkeypatch.setattr(tor_service, "BIN_DIR", tmp_path)
    cached = tmp_path / tor_service._binary_name()
    cached.write_text("")
    assert tor_service.resolve_binary() == str(cached)


def test_build_torrc_is_loopback_only_and_isolated():
    rc = tor_service.build_torrc(9999)
    assert "SocksPort 127.0.0.1:9999" in rc
    assert "SocksPolicy reject *" in rc        # only loopback may use the proxy
    assert "ClientOnly 1" in rc
    assert "DataDirectory" in rc and "Log notice file" in rc


def test_version_newer():
    assert tor_service._version_newer("13.5", "13.0") is True
    assert tor_service._version_newer("13.5.1", "13.5") is True
    assert tor_service._version_newer("13.0", "13.0") is False
    assert tor_service._version_newer("13.0", None) is True       # nothing installed -> newer


def test_active_proxy_none_when_not_running(monkeypatch):
    monkeypatch.setattr(tor_service, "_proc", None)
    monkeypatch.setattr(tor_service, "_bootstrapped", False)
    assert tor_service.active_proxy() is None


class _FakeProc:
    def poll(self):
        return None    # alive


def test_active_proxy_routes_as_soon_as_running(monkeypatch):
    # The WinError-10061 fix: route through managed Tor while it's up, even mid-bootstrap, instead
    # of falling back to a (possibly closed) manual proxy.
    monkeypatch.setattr(tor_service, "_proc", _FakeProc())
    monkeypatch.setattr(tor_service, "_socks_port", 58089)
    monkeypatch.setattr(tor_service, "_bootstrapped", False)
    assert tor_service.active_proxy() == ("127.0.0.1", 58089)


def test_is_ready_is_dynamic(monkeypatch):
    # A slow cold start that outran the initial wait must still flip to ready (not latch False).
    monkeypatch.setattr(tor_service, "_proc", _FakeProc())
    monkeypatch.setattr(tor_service, "_bootstrapped", False)
    monkeypatch.setattr(tor_service, "_log_bootstrapped", lambda: True)
    assert tor_service.is_ready() is True


def test_start_is_noop_when_managed_disabled(monkeypatch):
    monkeypatch.delenv("BTT_MANAGED_TOR", raising=False)
    # Should bail out before touching any binary/download.
    assert tor_service.start() is False


def test_node_settings_proxy_prefers_managed(monkeypatch):
    # When managed Tor is up, its ephemeral SOCKS wins over the configured proxy.
    monkeypatch.setattr(tor_service, "active_proxy", lambda: ("127.0.0.1", 9051))
    assert node_settings.tor_proxy_or("127.0.0.1", 9050) == ("127.0.0.1", 9051)
    # When it's not up, fall back to the configured host/port.
    monkeypatch.setattr(tor_service, "active_proxy", lambda: None)
    assert node_settings.tor_proxy_or("10.0.0.9", 9150) == ("10.0.0.9", 9150)


def test_check_update_flags_newer(monkeypatch):
    monkeypatch.setattr(tor_service, "latest_version", lambda: "99.9")
    monkeypatch.setattr(tor_service, "installed_version", lambda: "13.0")
    info = tor_service.check_update()
    assert info["update_available"] is True and info["latest"] == "99.9"
    monkeypatch.setattr(tor_service, "latest_version", lambda: "13.0")
    assert tor_service.check_update()["update_available"] is False


def test_status_shape():
    s = tor_service.status()
    assert {"enabled", "running", "bootstrapped", "socks_port", "version"} <= set(s)
