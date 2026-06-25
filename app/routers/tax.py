# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Tax forms: Form 8949 + Schedule D per account, with CSV export."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Transaction
from app.services import accounts as accounts_svc
from app.services import costbasis, node_settings, taxforms
from app.services import transactions as tx_svc
from app.templating import templates

router = APIRouter()


@router.get("/tax", response_class=HTMLResponse)
async def tax_home(request: Request, session: Session = Depends(get_session)):
    # Instance-wide filing-readiness: missing/estimated USD values across all txs + any
    # unmatched self-transfers awaiting review. (Per-account engine warnings appear on the 8949.)
    all_txs = list(session.scalars(select(Transaction)))
    flags = taxforms.readiness_flags(
        all_txs, costbasis.CostBasisResult(),
        price_source=node_settings.get_config(session).price_source,
        unreconciled=len(costbasis.suggest_transfers(session)))
    return templates.TemplateResponse(
        request, "tax.html",
        {"summaries": accounts_svc.all_summaries(session), "flags": flags},
    )


def _resolve(session: Session, account_id: int, request: Request):
    account = accounts_svc.get_account(session, account_id)
    if account is None:
        return None, None, None, None
    txs = tx_svc.list_transactions(session, account_id)
    cb = costbasis.compute_account(session, account_id)
    return account, txs, cb, taxforms.years_present(cb)


@router.get("/tax/{account_id}/8949", response_class=HTMLResponse)
async def form_8949(account_id: int, request: Request, year: int | None = None,
                    session: Session = Depends(get_session)):
    account, txs, cb, years = _resolve(session, account_id, request)
    if account is None:
        return RedirectResponse("/tax", status_code=303)
    if year is None and years:
        year = years[-1]
    rows = taxforms.build_rows(cb, year)
    flags = taxforms.readiness_flags(txs, cb, price_source=node_settings.get_config(session).price_source)
    return templates.TemplateResponse(
        request, "form_8949.html",
        {"account": account, "rows": rows, "totals": taxforms.totals(rows),
         "by_kyc": taxforms.totals_by_kyc(rows),
         "year": year, "years": years,
         "income": taxforms.income_for_year(txs, year) if year else None,
         "flags": flags},
    )


@router.get("/tax/{account_id}/8949.csv", response_class=PlainTextResponse)
async def form_8949_csv(account_id: int, request: Request, year: int | None = None,
                        session: Session = Depends(get_session)):
    account, txs, cb, years = _resolve(session, account_id, request)
    if account is None:
        return RedirectResponse("/tax", status_code=303)
    if year is None and years:
        year = years[-1]
    rows = taxforms.build_rows(cb, year)
    csv_text = taxforms.to_csv(rows, account.name, year, lot_method=account.lot_method,
                               price_source=node_settings.get_config(session).price_source)
    fname = f"form8949_{account.name.replace(' ', '_')}_{year or 'all'}.csv"
    return PlainTextResponse(
        csv_text, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
