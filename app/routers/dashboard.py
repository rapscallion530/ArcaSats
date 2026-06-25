# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Dashboard: portfolio overview across accounts."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import accounts as accounts_svc
from app.services import node_settings
from app.templating import templates

router = APIRouter()


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse(request, "about.html", {})


@router.get("/node/status", response_class=HTMLResponse)
async def node_status(request: Request, session: Session = Depends(get_session)):
    cfg = node_settings.get_config(session)
    result = node_settings.test_connection(session, timeout=12)
    summaries = accounts_svc.all_summaries(session)
    return templates.TemplateResponse(
        request, "partials/node_widget.html",
        {
            "result": result,
            "via_tor": cfg.use_tor or cfg.electrum_host.endswith(".onion"),
            "configured": bool(cfg.electrum_host.strip()),
            "account_count": len(summaries),
            "wallet_count": sum(s.wallet_count for s in summaries),
            "total_sats": sum(s.balance_sats for s in summaries),
            "tx_count": sum(s.tx_count for s in summaries),
        },
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: Session = Depends(get_session)):
    summaries = accounts_svc.all_summaries(session)
    total_sats = sum(s.balance_sats for s in summaries)
    total_wallets = sum(s.wallet_count for s in summaries)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "summaries": summaries,
            "total_sats": total_sats,
            "total_wallets": total_wallets,
            "account_count": len(summaries),
        },
    )
