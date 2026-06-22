# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""bitcoin-tax-tracker — local-only Bitcoin tax & accounting.

FastAPI + HTMX + Tailwind. Single-container, SQLite-backed, no data leaves the box.

Auth model: "open mode" when no users exist (no login required — single-user/local),
"secured mode" once an admin is created via /setup (login required, accounts scoped
to their owner). This keeps the app frictionless until you opt into multi-user.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import APP_NAME
from app.db import SessionLocal, init_db
from app.routers import (
    accounts,
    assistant as assistant_router,
    auth as auth_router,
    dashboard,
    settings as settings_router,
    tax,
)
from app.services import auth as auth_svc

BASE_DIR = Path(__file__).resolve().parent

# Paths reachable without a session in secured mode.
_PUBLIC_EXACT = {"/login", "/setup", "/logout", "/about", "/health", "/favicon.ico"}
# Paths that need no DB/auth work at all — skip the session entirely (the big per-request win).
_NO_DB_EXACT = {"/health", "/favicon.ico"}
_NO_DB_PREFIXES = ("/static",)
# Methods that change state and therefore get a same-origin (CSRF) check.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_NO_DB_PREFIXES)


def _needs_no_db(path: str) -> bool:
    return path in _NO_DB_EXACT or path.startswith(_NO_DB_PREFIXES)


def _enforce_setup_safety() -> None:
    """Hard boundary against open-mode takeover. If bound to a non-loopback interface (Docker
    0.0.0.0, Umbrel/StartOS, LAN) while NO admin exists, the first network visitor could claim
    admin. Refuse to start unless the operator provides an out-of-band BTT_SETUP_TOKEN (which
    then gates /setup) — or binds to loopback, or creates the admin first."""
    bind = os.environ.get("BTT_BIND_HOST", "127.0.0.1")
    if bind in ("127.0.0.1", "localhost", "::1"):
        return
    if os.environ.get("BTT_SETUP_TOKEN", ""):
        return  # setup is token-gated (see routers/auth.py)
    if os.environ.get("BTT_ALLOW_OPEN_EXPOSURE", "0") == "1":
        # Escape hatch for platforms that put their OWN authenticated gateway in front of the
        # app (StartOS/Umbrel). Set only when access to this port is already authenticated.
        return
    with SessionLocal() as session:
        open_mode = auth_svc.user_count(session) == 0
    if open_mode:
        raise RuntimeError(
            f"Refusing to start: bound to {bind} (non-loopback) with no admin user and no "
            "BTT_SETUP_TOKEN. Anyone reaching the port could claim admin. Bind to 127.0.0.1, "
            "create the admin first, or set BTT_SETUP_TOKEN to a secret and open "
            "/setup?token=<secret> to bootstrap.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _enforce_setup_safety()
    yield


app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Attach request.state.user_* , gate app routes in secured mode, and apply a same-origin
    (CSRF) check to state-changing requests. Static/health requests skip the DB entirely."""
    path = request.url.path
    request.state.user_id = None
    request.state.username = None
    request.state.role = None
    request.state.secured = False

    # CSRF defense-in-depth (alongside SameSite=lax cookies): reject a state-changing request
    # whose Origin/Referer host doesn't match ours. Missing header (non-browser clients,
    # tests) is allowed; a *mismatched* one is blocked.
    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            origin_host = urlparse(origin).netloc
            if origin_host and origin_host != request.headers.get("host"):
                return PlainTextResponse("Cross-origin request blocked.", status_code=403)

    # Skip all auth/DB work for static assets, health, favicon — the bulk of requests.
    if _needs_no_db(path):
        return await call_next(request)

    with SessionLocal() as session:
        # A COUNT on the tiny users table; cheap now that /static et al. skip this entirely.
        if auth_svc.user_count(session) > 0:
            request.state.secured = True
            decoded = auth_svc.decode_token(request.cookies.get("btt_session"))
            if decoded is not None:
                uid, _issued, ver = decoded
                from app.models import User
                user = session.get(User, uid)
                # Reject if the user is gone or their token_version was bumped (revoked).
                if user is not None and user.token_version == ver:
                    request.state.user_id = user.id
                    request.state.username = user.username
                    request.state.role = user.role

    if request.state.secured and request.state.user_id is None and not _is_public(path):
        return RedirectResponse("/login", status_code=303)

    return await call_next(request)


app.include_router(auth_router.router)
app.include_router(dashboard.router)
app.include_router(accounts.router)
app.include_router(tax.router)
app.include_router(settings_router.router)
app.include_router(assistant_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
