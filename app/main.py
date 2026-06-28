# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""bitcoin-tax-tracker — local-only Bitcoin tax & accounting.

FastAPI + HTMX + Tailwind. Single-container, SQLite-backed, no data leaves the box.

Single-user by design: there are no user accounts. The app is open by default; set
BTT_APP_PASSWORD to gate the whole instance behind one password (an HMAC-signed cookie marks
a session unlocked). Real network exposure should sit behind a platform auth gateway
(StartOS/Umbrel) — see _enforce_exposure_safety.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import APP_NAME
from app.db import init_db
from app.routers import (
    accounts,
    assistant as assistant_router,
    auth as auth_router,
    dashboard,
    reconcile as reconcile_router,
    settings as settings_router,
    tax,
)
from app.services import auth as auth_svc

BASE_DIR = Path(__file__).resolve().parent

# Paths reachable without unlocking when the optional password lock is on.
_PUBLIC_EXACT = {"/login", "/logout", "/about", "/health", "/favicon.ico"}
# Paths that need no work at all — skip everything (the big per-request win).
_NO_DB_EXACT = {"/health", "/favicon.ico"}
_NO_DB_PREFIXES = ("/static",)
# Methods that change state and therefore get a same-origin (CSRF) check.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_NO_DB_PREFIXES)


def _needs_no_db(path: str) -> bool:
    return path in _NO_DB_EXACT or path.startswith(_NO_DB_PREFIXES)


def _enforce_exposure_safety() -> None:
    """Refuse to start wide-open on a non-loopback interface. If bound to anything other than
    loopback (Docker 0.0.0.0, LAN, Umbrel/StartOS) with NO app password, anyone reaching the
    port has full access. Require either BTT_APP_PASSWORD, or BTT_ALLOW_OPEN_EXPOSURE=1 for a
    platform that fronts the app with its own authenticated gateway."""
    bind = os.environ.get("BTT_BIND_HOST", "127.0.0.1")
    if bind in ("127.0.0.1", "localhost", "::1"):
        return
    if os.environ.get("BTT_APP_PASSWORD", ""):
        return  # the password lock protects it
    if os.environ.get("BTT_ALLOW_OPEN_EXPOSURE", "0") == "1":
        return  # a platform gateway (StartOS/Umbrel) authenticates access to this port
    raise RuntimeError(
        f"Refusing to start: bound to {bind} (non-loopback) with no BTT_APP_PASSWORD. Anyone "
        "reaching the port would have full access. Bind to 127.0.0.1, set BTT_APP_PASSWORD to "
        "gate the app, or set BTT_ALLOW_OPEN_EXPOSURE=1 if an authenticated gateway fronts it.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _enforce_exposure_safety()
    yield


app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.middleware("http")
async def gate_middleware(request: Request, call_next):
    """Apply a same-origin (CSRF) check to state-changing requests, and — when the optional
    password lock is enabled — require a valid unlock cookie on non-public routes."""
    path = request.url.path

    # CSRF defense-in-depth (alongside SameSite=lax cookies): reject a state-changing request
    # whose Origin/Referer host doesn't match ours. Missing header (non-browser clients,
    # tests) is allowed; a *mismatched* one is blocked.
    if request.method in _UNSAFE_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            origin_host = urlparse(origin).netloc
            if origin_host and origin_host != request.headers.get("host"):
                return PlainTextResponse("Cross-origin request blocked.", status_code=403)

    if _needs_no_db(path):
        return await call_next(request)

    # Optional single-password lock: gate everything except public routes until unlocked.
    if auth_svc.app_lock_enabled() and not _is_public(path):
        if not auth_svc.verify_unlock(request.cookies.get("btt_session")):
            return RedirectResponse("/login", status_code=303)

    return await call_next(request)


app.include_router(auth_router.router)
app.include_router(dashboard.router)
app.include_router(accounts.router)
app.include_router(reconcile_router.router)
app.include_router(tax.router)
app.include_router(settings_router.router)
app.include_router(assistant_router.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
