"""Phase 7: multi-user auth (open mode -> secured mode) + account scoping."""
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account, User
from app.services import auth as auth_svc


@pytest.fixture
def clean_users():
    """Remove all users + accounts after the test so other tests stay in open mode."""
    yield
    with SessionLocal() as s:
        for row in s.query(Account).all():
            s.delete(row)
        for row in s.query(User).all():
            s.delete(row)
        s.commit()
    auth_svc.get_secret_key.cache_clear()


# --- pure unit tests (no DB state) ---
def test_password_hash_roundtrip():
    h = auth_svc.hash_password("hunter2")
    assert auth_svc.verify_password("hunter2", h)
    assert not auth_svc.verify_password("wrong", h)


def test_token_sign_verify():
    tok = auth_svc.sign_token(42)
    assert auth_svc.verify_token(tok) == 42
    assert auth_svc.verify_token(tok + "tamper") is None
    assert auth_svc.verify_token(None) is None


# --- flow tests ---
def test_open_mode_requires_no_login(client):
    assert client.get("/").status_code == 200


def test_setup_then_secured(client, clean_users):
    from app.main import app

    r = client.post("/setup", data={"username": "uncle", "password": "secret1"}, follow_redirects=False)
    assert r.status_code == 303  # created admin + signed in (cookie on `client`)
    assert client.get("/").status_code == 200

    # a fresh client with no cookie is bounced to /login
    anon = TestClient(app)
    bounced = anon.get("/", follow_redirects=False)
    assert bounced.status_code == 303 and bounced.headers["location"] == "/login"

    # wrong password rejected
    assert anon.post("/login", data={"username": "uncle", "password": "nope"}).status_code == 401
    # correct password works
    ok = anon.post("/login", data={"username": "uncle", "password": "secret1"}, follow_redirects=False)
    assert ok.status_code == 303
    assert anon.get("/").status_code == 200


def test_member_sees_only_own_accounts(client, clean_users):
    from app.main import app

    # admin
    client.post("/setup", data={"username": "admin", "password": "secret1"})
    client.post("/accounts", data={"name": "AdminAcct"})

    # member
    with SessionLocal() as s:
        auth_svc.create_user(s, "kid", "pw123456", role="member")
    member = TestClient(app)
    member.post("/login", data={"username": "kid", "password": "pw123456"})
    member.post("/accounts", data={"name": "KidAcct"})

    member_view = member.get("/accounts").text
    assert "KidAcct" in member_view
    assert "AdminAcct" not in member_view   # scoped out

    admin_view = client.get("/accounts").text
    assert "AdminAcct" in admin_view and "KidAcct" in admin_view  # admin sees all
