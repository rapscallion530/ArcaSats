# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Reconciliation inbox: review suggested same-owner self-transfers that share no txid.

Suggestions are never auto-applied — the user confirms (relabel both sides to transfers +
carry basis) or rejects (genuine external buy/sell).
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import TxKind
from app.services import costbasis, node_settings
from app.templating import templates

router = APIRouter()


def _ctx(session: Session):
    return {"suggestions": costbasis.suggest_transfers(session), "labels": TxKind.LABELS,
            "mempool_url": node_settings.get_config(session).mempool_url}


def _list(request: Request, session: Session):
    return templates.TemplateResponse(request, "partials/reconcile_list.html", _ctx(session))


@router.get("/reconcile", response_class=HTMLResponse)
async def reconcile_inbox(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "reconcile.html", _ctx(session))


@router.post("/reconcile/confirm", response_class=HTMLResponse)
async def reconcile_confirm(request: Request, out_tx_id: int = Form(...), in_tx_id: int = Form(...),
                            session: Session = Depends(get_session)):
    costbasis.confirm_transfer(session, out_tx_id, in_tx_id)
    return _list(request, session)


@router.post("/reconcile/reject", response_class=HTMLResponse)
async def reconcile_reject(request: Request, out_tx_id: int = Form(...), in_tx_id: int = Form(...),
                           session: Session = Depends(get_session)):
    costbasis.reject_suggestion(session, out_tx_id, in_tx_id)
    return _list(request, session)
