"""http_fetch: clearnet (urllib) + Tor (SOCKS5 over a raw socket) JSON GET, with no real network."""
from app.services import http_fetch


class _Resp:
    def __init__(self, payload: bytes):
        self._p = payload
    def read(self):
        return self._p
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeSock:
    """Records what's sent and replays a canned HTTP response in recv() chunks."""
    def __init__(self, response: bytes):
        self._resp = response
        self.sent = b""
    def settimeout(self, _):
        pass
    def sendall(self, b):
        self.sent += b
    def recv(self, n):
        chunk, self._resp = self._resp[:n], self._resp[n:]
        return chunk
    def close(self):
        pass


def test_clearnet_parses_json(monkeypatch):
    monkeypatch.setattr(http_fetch.urllib.request, "urlopen", lambda *a, **k: _Resp(b'{"prices": [{"USD": 7}]}'))
    assert http_fetch.get_json("http://m.local:3006/api") == {"prices": [{"USD": 7}]}


def test_tor_path_uses_socks_and_preserves_path(monkeypatch):
    captured = {}
    sock = _FakeSock(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                     b"Connection: close\r\n\r\n{\"prices\": [{\"USD\": 42}]}")

    def fake_create_connection(addr, timeout=None):
        captured["proxy"] = addr
        return sock

    monkeypatch.setattr(http_fetch.socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(http_fetch, "_socks5_connect",
                        lambda s, host, port: captured.setdefault("dest", (host, port)))

    out = http_fetch.get_json(
        "http://abcd.onion:3006/api/v1/historical-price?currency=USD&timestamp=1",
        proxy_host="127.0.0.1", proxy_port=9050)

    assert out == {"prices": [{"USD": 42}]}
    assert captured["proxy"] == ("127.0.0.1", 9050)         # connected to the SOCKS proxy
    assert captured["dest"] == ("abcd.onion", 3006)         # SOCKS CONNECT to the mempool host:port
    assert b"GET /api/v1/historical-price?currency=USD&timestamp=1 HTTP/1.1" in sock.sent
    assert b"Connection: close" in sock.sent


def test_tor_path_chunked_response(monkeypatch):
    # nginx-style chunked body must still decode. One 0x18 (24-byte) chunk, then the 0 terminator.
    body = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"18\r\n" + b'{"prices": [{"USD": 9}]}' + b"\r\n0\r\n\r\n")
    monkeypatch.setattr(http_fetch.socket, "create_connection", lambda *a, **k: _FakeSock(body))
    monkeypatch.setattr(http_fetch, "_socks5_connect", lambda *a, **k: None)
    assert http_fetch.get_json("http://x.onion/api", proxy_host="127.0.0.1", proxy_port=9050) == {"prices": [{"USD": 9}]}


def test_via_tor_rule():
    assert http_fetch.via_tor("abcd.onion", False) is True       # .onion always over Tor
    assert http_fetch.via_tor("m.local", True) is True           # explicit opt-in
    assert http_fetch.via_tor("m.local", False) is False         # clearnet/LAN direct
