# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Reconciliation inbox: review suggested same-owner self-transfers that share no txid.

Suggestions are never auto-applied — the user confirms (relabel both sides to transfers +
carry basis) or rejects (genuine external buy/sell). Owner-scoped in secured mode.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import TxKind
from app.services import costbasis
from app.templating import templates

router = APIRouter()


def _list(request: Request, session: Session):
    suggestions = costbasis.suggest_transfers(session, request.state.user_id, request.state.role)
    return templates.TemplateResponse(request, "partials/reconcile_list.html",
                                      {"suggestions": suggestions, "labels": TxKind.LABELS})


@router.get("/reconcile", response_class=HTMLResponse)
async def reconcile_inbox(request: Request, session: Session = Depends(get_session)):
    suggestions = costbasis.suggest_transfers(session, request.state.user_id, request.state.role)
    return templates.TemplateResponse(request, "reconcile.html",
                                      {"suggestions": suggestions, "labels": TxKind.LABELS})


@router.post("/reconcile/confirm", response_class=HTMLResponse)
async def reconcile_confirm(request: Request, out_tx_id: int = Form(...), in_tx_id: int = Form(...),
                            session: Session = Depends(get_session)):
    costbasis.confirm_transfer(session, out_tx_id, in_tx_id,
                               request.state.user_id, request.state.role)
    return _list(request, session)


@router.post("/reconcile/reject", response_class=HTMLResponse)
async def reconcile_reject(request: Request, out_tx_id: int = Form(...), in_tx_id: int = Form(...),
                           session: Session = Depends(get_session)):
    costbasis.reject_suggestion(session, out_tx_id, in_tx_id,
                                request.state.user_id, request.state.role)
    return _list(request, session)
