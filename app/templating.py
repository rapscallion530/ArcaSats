# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Shared Jinja2 templates instance + formatting filters."""
from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app import config
from app.models import SATS_PER_BTC
from app.services import auth

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)
templates.env.globals["ASSETS"] = config.ASSETS
templates.env.globals["APP_NAME"] = config.APP_NAME
templates.env.globals["TAGLINE"] = config.TAGLINE
templates.env.globals["SUPPORT_URL"] = config.SUPPORT_URL
# Whether the optional single-password lock is on (drives the "Lock & sign out" control).
templates.env.globals["app_locked"] = auth.app_lock_enabled()


def fmt_btc(sats: int | None) -> str:
    if sats is None:
        return "—"
    return f"{Decimal(int(sats)) / SATS_PER_BTC:.8f}"


def fmt_btc_amt(sats: int | None) -> Markup:
    """A displayed amount, default-rendered in BTC but carrying its raw sat value so the
    client-side units toggle (BTC <-> sats) can re-render it without a round-trip. Use this
    for read-only displays; use the plain `btc` filter where a raw decimal string is needed
    (e.g. inside a form input value)."""
    if sats is None:
        return Markup("—")
    return Markup(f'<span class="amt" data-sats="{int(sats)}">{escape(fmt_btc(sats))}</span>')


def fmt_usd(value) -> str:
    if value is None or value == "":
        return "—"
    return f"${Decimal(value):,.2f}"


def fmt_signed_usd(value) -> str:
    if value is None:
        return "—"
    d = Decimal(value)
    sign = "-" if d < 0 else ""
    return f"{sign}${abs(d):,.2f}"


templates.env.filters["btc"] = fmt_btc
templates.env.filters["btcamt"] = fmt_btc_amt
templates.env.filters["usd"] = fmt_usd
templates.env.filters["susd"] = fmt_signed_usd
