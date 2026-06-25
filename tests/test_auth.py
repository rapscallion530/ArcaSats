"""Single-user model: password hashing + the optional app-wide password lock."""
import time

from fastapi.testclient import TestClient  # noqa: F401  (kept for ad-hoc client construction)

from app.services import auth as auth_svc


def test_password_hash_roundtrip():
    h = auth_svc.hash_password("hunter2")
    assert auth_svc.verify_password("hunter2", h)
    assert not auth_svc.verify_password("wrong", h)


def test_unlock_token_sign_verify():
    tok = auth_svc.sign_unlock()
    assert auth_svc.verify_unlock(tok)
    assert not auth_svc.verify_unlock(tok + "tamper")
    assert not auth_svc.verify_unlock(None)
    # Issued far in the past -> expired -> rejected.
    old = auth_svc.sign_unlock(issued_at=int(time.time()) - 10**9)
    assert not auth_svc.verify_unlock(old)


def test_no_lock_means_open(client):
    # BTT_APP_PASSWORD unset (conftest) -> app is open, no redirect to /login.
    assert client.get("/").status_code == 200
    # /login just bounces to the dashboard when there's nothing to unlock.
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_app_password_lock(client, monkeypatch):
    # With BTT_APP_PASSWORD set, unauthenticated requests are bounced to /login until unlocked.
    monkeypatch.setenv("BTT_APP_PASSWORD", "letmein")
    bounced = client.get("/", follow_redirects=False)
    assert bounced.status_code == 303 and bounced.headers["location"] == "/login"
    # Wrong password rejected.
    assert client.post("/login", data={"password": "nope"}).status_code == 401
    # Correct password sets the unlock cookie (on the client) and grants access.
    ok = client.post("/login", data={"password": "letmein"}, follow_redirects=False)
    assert ok.status_code == 303
    assert client.get("/").status_code == 200
