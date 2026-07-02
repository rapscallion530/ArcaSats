# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
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


def _combined(session: Session, year: int | None):
    """All accounts' disposals aggregated into one 8949/Schedule D for `year`. Each account's lots
    are selected WITHIN that account (Rev. Proc. 2024-28) — we sum the per-account results, never
    re-pool lots across accounts."""
    from decimal import Decimal
    per = [(a, costbasis.compute_account(session, a.id), tx_svc.list_transactions(session, a.id))
           for a in accounts_svc.list_accounts(session)]
    years = sorted({y for _, cb, _ in per for y in taxforms.years_present(cb)})
    if year is None and years:
        year = years[-1]
    rows, all_txs, warnings, income = [], [], [], Decimal("0")
    for a, cb, txs in per:
        all_txs.extend(txs)
        warnings.extend(cb.warnings)
        for r in taxforms.build_rows(cb, year):
            r.account = a.name
            rows.append(r)
        if year:
            income += taxforms.income_for_year(txs, year)
    rows.sort(key=lambda r: (r.term, r.sold, r.account))
    return rows, all_txs, warnings, income, year, years


@router.get("/tax/combined", response_class=HTMLResponse)
async def tax_combined(request: Request, year: int | None = None,
                       session: Session = Depends(get_session)):
    rows, all_txs, warnings, income, year, years = _combined(session, year)
    combo = costbasis.CostBasisResult()
    combo.warnings = warnings
    flags = taxforms.readiness_flags(
        all_txs, combo, price_source=node_settings.get_config(session).price_source,
        unreconciled=len(costbasis.suggest_transfers(session)))
    return templates.TemplateResponse(
        request, "tax_combined.html",
        {"rows": rows, "totals": taxforms.totals(rows), "by_kyc": taxforms.totals_by_kyc(rows),
         "by_account": taxforms.totals_by_account(rows), "income": income if year else None,
         "year": year, "years": years, "flags": flags},
    )


@router.get("/tax/combined.csv", response_class=PlainTextResponse)
async def tax_combined_csv(request: Request, year: int | None = None,
                          session: Session = Depends(get_session)):
    rows, _txs, _w, _inc, year, _years = _combined(session, year)
    csv_text = taxforms.to_csv_combined(rows, year, price_source=node_settings.get_config(session).price_source)
    fname = f"form8949_ALL_{year or 'all'}.csv"
    return PlainTextResponse(csv_text, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


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
