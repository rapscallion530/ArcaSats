"""Native-window launcher (desktop.py): port selection + start/stop lifecycle, with the GUI and
the uvicorn server stubbed so no window or real server is needed."""
import webbrowser

import desktop


def test_free_port_is_bindable():
    import socket
    p = desktop._free_port(0)
    assert isinstance(p, int) and p > 0
    with socket.socket() as s:           # the ephemeral port we returned is actually free to bind
        s.bind((desktop.HOST, p))


class _FakeServer:
    def __init__(self):
        self.started = True
        self.should_exit = False


class _FakeThread:
    def join(self, timeout=None):
        pass


def test_main_stops_server_after_a_real_window_session(monkeypatch):
    # A genuine window session (>= min_session_s) ends -> stop the server, no browser fallback.
    fake = _FakeServer()
    monkeypatch.setattr(desktop, "_start_server", lambda port: (fake, _FakeThread()))
    monkeypatch.setattr(webbrowser, "open", lambda u: (_ for _ in ()).throw(AssertionError("no browser")))
    seen = {}
    rc = desktop.main(run_window=lambda url: seen.update(url=url), min_session_s=0.0)
    assert rc == 0
    assert seen["url"].startswith("http://127.0.0.1:")
    assert fake.should_exit is True


def test_main_falls_back_to_browser_when_window_fails(monkeypatch):
    fake = _FakeServer()
    monkeypatch.setattr(desktop, "_start_server", lambda port: (fake, _FakeThread()))
    opened = {}
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.update(u=u))

    def boom(url):
        raise RuntimeError("no display")

    rc = desktop.main(run_window=boom)
    assert rc == 0
    assert opened["u"].startswith("http://127.0.0.1:")              # degraded to a browser tab
    assert fake.should_exit is True


def test_existing_instance_none_without_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "_lock_path", lambda: str(tmp_path / "desktop.lock"))
    assert desktop.existing_instance_url() is None


def test_lock_write_then_stale_is_ignored(monkeypatch, tmp_path):
    # A lock pointing at a port nobody is serving is treated as stale (so we start fresh).
    monkeypatch.setattr(desktop, "_lock_path", lambda: str(tmp_path / "desktop.lock"))
    desktop._write_lock("http://127.0.0.1:65530")
    assert desktop.existing_instance_url() is None


def test_main_falls_back_when_window_returns_immediately(monkeypatch):
    # The actual reported bug: webview.start() returns at once WITHOUT raising (window never shows).
    # Must not silently exit — open a browser tab instead.
    fake = _FakeServer()
    monkeypatch.setattr(desktop, "_start_server", lambda port: (fake, _FakeThread()))
    opened = {}
    monkeypatch.setattr(webbrowser, "open", lambda u: opened.update(u=u))
    rc = desktop.main(run_window=lambda url: None, min_session_s=10.0)   # "instant" return
    assert rc == 0
    assert opened["u"].startswith("http://127.0.0.1:")
