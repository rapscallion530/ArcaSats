# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Authentication: password hashing + an optional single app-wide lock (stdlib only).

ArcaSats is single-user and local-only — there are no user accounts. When BTT_APP_PASSWORD is
set, the whole app is gated behind that one password (useful if you expose the instance); when
it's unset (the default), the app is open. An HMAC-signed cookie marks a session as unlocked.
No native deps — PBKDF2-HMAC-SHA256 primitives are kept; the secret key is read from
BTT_SECRET_KEY or persisted to the data dir on first run.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from functools import lru_cache

from app import config

_ITER = 200_000
# An unlock cookie is valid for 30 days (matches the cookie max-age); server-side expiry means
# a leaked cookie can't be replayed forever.
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
    # This key signs the unlock cookie — keep it owner-only (best effort; no-op on Windows ACLs).
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _sig(payload: str) -> str:
    return hmac.new(get_secret_key(), payload.encode(), hashlib.sha256).hexdigest()


# --- optional single app-wide password lock ----------------------------------
def app_lock_enabled() -> bool:
    """True when BTT_APP_PASSWORD is set, i.e. the app requires the password to enter."""
    return bool(os.environ.get("BTT_APP_PASSWORD", ""))


def check_app_password(pw: str) -> bool:
    """Constant-time compare against the configured app password (False if none set)."""
    expected = os.environ.get("BTT_APP_PASSWORD", "")
    return bool(expected) and hmac.compare_digest(pw, expected)


def sign_unlock(issued_at: int | None = None) -> str:
    """Signed 'unlocked' cookie payload: `issued_at.sig`. No user identity — the app is
    single-user; the cookie only attests that the password was entered."""
    issued = int(issued_at if issued_at is not None else time.time())
    return f"{issued}.{_sig(str(issued))}"


def verify_unlock(token: str | None, max_age: int = _TOKEN_MAX_AGE) -> bool:
    """True iff `token` is a validly-signed, unexpired unlock cookie."""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 2:
        return False
    issued_s, sig = parts
    if not hmac.compare_digest(sig, _sig(issued_s)):
        return False
    try:
        issued = int(issued_s)
    except ValueError:
        return False
    return int(time.time()) - issued <= max_age
