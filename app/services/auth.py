# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Authentication: password hashing + signed session tokens (stdlib only).

No native deps (bcrypt/argon2 need wheels). PBKDF2-HMAC-SHA256 for passwords,
HMAC-signed cookie tokens. Secret key is read from BTT_SECRET_KEY or persisted to
the data dir on first run.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from functools import lru_cache

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.models import User

_ITER = 200_000
# Session tokens are valid for 30 days (matches the cookie max-age). Server-side expiry means
# a leaked cookie can't be replayed forever; token_version (signed in) allows revoking all of a
# user's sessions (e.g. on password change) without rotating the global secret.
_TOKEN_MAX_AGE = 60 * 60 * 24 * 30


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _algo, it, salt_hex, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), int(it))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), h)


@lru_cache(maxsize=1)
def get_secret_key() -> bytes:
    env = os.environ.get("BTT_SECRET_KEY")
    if env:
        return env.encode()
    path = config.DATA_DIR / "secret.key"
    if path.exists():
        return path.read_bytes()
    key = os.urandom(32)
    path.write_bytes(key)
    # This key forges any session — keep it owner-only (best effort; no-op on Windows ACLs).
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _sig(payload: str) -> str:
    return hmac.new(get_secret_key(), payload.encode(), hashlib.sha256).hexdigest()


def sign_token(user_id: int, token_version: int = 0, issued_at: int | None = None) -> str:
    """Signed cookie payload: `uid.issued_at.token_version.sig`. issued_at/token_version let
    the server expire and revoke sessions; the signature covers all three fields."""
    issued = int(issued_at if issued_at is not None else time.time())
    payload = f"{user_id}.{issued}.{token_version}"
    return f"{payload}.{_sig(payload)}"


def decode_token(token: str | None, max_age: int = _TOKEN_MAX_AGE) -> tuple[int, int, int] | None:
    """Validate signature + age. Returns (user_id, issued_at, token_version) or None.
    The caller still checks token_version against the user's current value (revocation)."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    uid_s, issued_s, ver_s, sig = parts
    if not hmac.compare_digest(sig, _sig(f"{uid_s}.{issued_s}.{ver_s}")):
        return None
    try:
        uid, issued, ver = int(uid_s), int(issued_s), int(ver_s)
    except ValueError:
        return None
    if int(time.time()) - issued > max_age:
        return None  # expired
    return uid, issued, ver


def verify_token(token: str | None) -> int | None:
    """User id from a valid (signed, unexpired) token, else None. Does not check
    token_version — use decode_token + the user's current version for revocation."""
    decoded = decode_token(token)
    return decoded[0] if decoded else None


# --- user ops ---
def user_count(session: Session) -> int:
    return int(session.scalar(select(func.count(User.id))) or 0)


def create_user(session: Session, username: str, password: str, role: str = "member") -> User:
    u = User(username=username.strip(), password_hash=hash_password(password), role=role)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def authenticate(session: Session, username: str, password: str) -> User | None:
    u = session.scalar(select(User).where(User.username == username.strip()))
    if u and verify_password(password, u.password_hash):
        return u
    return None


def bump_token_version(session: Session, user: User) -> None:
    """Invalidate all of a user's existing sessions (e.g. after a password change or a
    "sign out everywhere"). Existing cookies carry the old version and will be rejected."""
    user.token_version = (user.token_version or 0) + 1
    session.commit()
