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


def test_main_stops_server_after_window_closes(monkeypatch):
    fake = _FakeServer()
    monkeypatch.setattr(desktop, "_start_server", lambda port: (fake, _FakeThread()))
    seen = {}
    rc = desktop.main(run_window=lambda url: seen.update(url=url))   # returns == user closed window
    assert rc == 0
    assert seen["url"].startswith("http://127.0.0.1:")
    assert fake.should_exit is True                                  # server told to stop


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
