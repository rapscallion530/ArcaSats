# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Auth routes: first-run setup, login, logout."""
import hmac
import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import auth
from app.templating import templates

router = APIRouter()
COOKIE = "btt_session"


def _setup_token_ok(supplied: str | None) -> bool:
    """When BTT_SETUP_TOKEN is configured (required to expose the app non-loopback in open
    mode — see main._enforce_setup_safety), /setup is gated by it so a random network visitor
    can't bootstrap the admin. No token configured (loopback/dev) => open."""
    token = os.environ.get("BTT_SETUP_TOKEN", "")
    if not token:
        return True
    return bool(supplied) and hmac.compare_digest(supplied, token)


def _set_session(resp: RedirectResponse, user, secure: bool = False) -> RedirectResponse:
    # Sign in the user's current token_version so the cookie can be revoked server-side.
    # `secure` is set when served over HTTPS (can't be unconditional — it would break the
    # default localhost-over-HTTP flow, where the cookie would then never be sent).
    token = auth.sign_token(user.id, user.token_version)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=secure,
                    max_age=60 * 60 * 24 * 30)
    return resp


@router.get("/setup", response_class=HTMLResponse)
async def setup_form(request: Request, session: Session = Depends(get_session)):
    if auth.user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    supplied = request.query_params.get("token")
    if not _setup_token_ok(supplied):
        return HTMLResponse("Setup requires a valid bootstrap token (open /setup?token=…).",
                            status_code=403)
    return templates.TemplateResponse(request, "setup.html", {"setup_token": supplied or ""})


@router.post("/setup")
async def setup(request: Request, username: str = Form(...), password: str = Form(...),
                token: str = Form(""), session: Session = Depends(get_session)):
    if auth.user_count(session) > 0:
        return RedirectResponse("/login", status_code=303)
    if not _setup_token_ok(token or request.query_params.get("token")):
        return HTMLResponse("Invalid or missing setup token.", status_code=403)
    user = auth.create_user(session, username, password, role="admin")
    return _set_session(RedirectResponse("/", status_code=303), user, secure=request.url.scheme == "https")


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, session: Session = Depends(get_session), error: str = ""):
    if auth.user_count(session) == 0:
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...),
                session: Session = Depends(get_session)):
    user = auth.authenticate(session, username, password)
    if user is None:
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid username or password."}, status_code=401)
    return _set_session(RedirectResponse("/", status_code=303), user, secure=request.url.scheme == "https")


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
