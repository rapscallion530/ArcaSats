# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Master ledger: every transaction across ALL accounts and wallets in one view (read-only),
with filters + CSV export. Cost basis / tax stay per-account (Rev. Proc. 2024-28); this is a
unified browse/audit view, not a re-pooled lot engine."""
import csv
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import TxKind
from app.services import accounts as accounts_svc
from app.services import transactions as tx_svc
from app.templating import templates

router = APIRouter()


def _int_or_none(value: str | None) -> int | None:
    """Coerce a query value to int, treating ""/None/non-numeric (the 'All' options) as no filter.
    The filter <select>s submit empty strings for 'All …', which an `int` param would 422 on."""
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def _filter(session: Session, account_id, kind, year):
    aid = _int_or_none(account_id)
    yr = _int_or_none(year)
    knd = kind if kind in TxKind.ALL else None
    return tx_svc.list_all(session, account_id=aid, kind=knd, year=yr), aid, knd, yr


@router.get("/ledger", response_class=HTMLResponse)
async def ledger(request: Request, account_id: str | None = None, kind: str | None = None,
                 year: str | None = None, session: Session = Depends(get_session)):
    txs, aid, knd, yr = _filter(session, account_id, kind, year)
    return templates.TemplateResponse(request, "ledger.html", {
        "txs": txs,
        "accounts": accounts_svc.list_accounts(session),
        "kinds": TxKind.ALL,
        "years": tx_svc.all_years(session),
        "sel_account": aid, "sel_kind": knd, "sel_year": yr,
        "mempool_url": accounts_svc_mempool(session),
    })


def accounts_svc_mempool(session: Session) -> str:
    """Explorer base URL for txid links (best-effort; blank if unset)."""
    try:
        from app.services import node_settings
        return (node_settings.get_config(session).mempool_url or "").rstrip("/")
    except Exception:  # noqa: BLE001
        return ""


@router.get("/ledger.csv", response_class=PlainTextResponse)
async def ledger_csv(request: Request, account_id: str | None = None, kind: str | None = None,
                     year: str | None = None, session: Session = Depends(get_session)):
    txs, _aid, _knd, _yr = _filter(session, account_id, kind, year)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Date (UTC)", "Account", "Wallet", "Type", "BTC", "USD value", "KYC",
                "Counterparty", "Txid"])
    for t in txs:
        w.writerow([
            t.timestamp.strftime("%Y-%m-%d %H:%M:%S") if t.timestamp else "",
            t.account.name if t.account else "",
            t.wallet.label if t.wallet else "",
            t.kind,
            f"{t.amount_sats / 1e8:.8f}",
            ("" if t.usd_value is None else f"{t.usd_value:.2f}"),
            t.kyc_origin or "",
            t.counterparty or "",
            t.txid or "",
        ])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=arcasats-ledger.csv"})
