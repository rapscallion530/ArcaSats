# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Auth routes: the optional single-password lock (login/logout). No user accounts.

When BTT_APP_PASSWORD is unset the app is open and these routes just bounce to the dashboard.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services import auth
from app.templating import templates

router = APIRouter()
COOKIE = "btt_session"
_MAX_AGE = 60 * 60 * 24 * 30  # 30 days; matches the signed unlock token's max age


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str = ""):
    if not auth.app_lock_enabled():
        return RedirectResponse("/", status_code=303)  # no lock configured — nothing to unlock
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login")
async def login(request: Request, password: str = Form(...)):
    if not auth.app_lock_enabled():
        return RedirectResponse("/", status_code=303)
    if not auth.check_app_password(password):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Incorrect password."}, status_code=401)
    resp = RedirectResponse("/", status_code=303)
    # `secure` only over HTTPS — unconditional would break the default localhost-over-HTTP flow.
    resp.set_cookie(COOKIE, auth.sign_unlock(), httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", max_age=_MAX_AGE)
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
