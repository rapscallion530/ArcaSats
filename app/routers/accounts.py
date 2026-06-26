# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Accounts: list, create, detail, delete + manual transaction entry.

Single-user app: handlers resolve account/wallet/tx targets by id (tx routes also require the
tx to belong to the URL's account). A missing/mismatched id yields an empty 404 partial (HTMX
renders nothing) or a redirect on a full-page GET.
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import config
from app.db import get_session
from app.models import TxKind, WalletType
from app.services import accounts as accounts_svc
from app.services import coins as coins_svc
from app.services import costbasis
from app.services import node_settings
from app.services import sync as sync_svc
from app.services import transactions as tx_svc
from app.services.importers import csv_import
from app.templating import templates

router = APIRouter()

# Cap CSV uploads so a huge/malicious file can't exhaust memory (the whole file is read in).
_MAX_CSV_BYTES = 10 * 1024 * 1024  # 10 MB — generous for a tax-year export

# IRS annual gift-tax exclusion by year (verify against current IRS guidance before filing).
# A gift's exclusion is the one in effect for the YEAR of the gift, not always the latest.
_GIFT_EXCLUSION = {2021: 15000, 2022: 16000, 2023: 17000, 2024: 18000, 2025: 19000, 2026: 19000}


def _gift_exclusion_for(year: int) -> tuple[Decimal, bool]:
    """(exclusion, known). For a year outside the table we clamp to the nearest known value but
    flag known=False so the UI can warn rather than present a fabricated threshold as fact."""
    if year in _GIFT_EXCLUSION:
        return Decimal(_GIFT_EXCLUSION[year]), True
    known = sorted(_GIFT_EXCLUSION)
    pick = known[0] if year < known[0] else known[-1]
    return Decimal(_GIFT_EXCLUSION[pick]), False

_DENIED_PARTIAL = HTMLResponse("", status_code=404)


# --- scope helpers (see module docstring) ------------------------------------
def _account(session: Session, account_id: int, request: Request):
    return accounts_svc.get_account(session, account_id)


def _wallet(session: Session, wallet_id: int, request: Request):
    return accounts_svc.get_wallet(session, wallet_id)


def _tx(session: Session, account_id: int, tx_id: int, request: Request):
    return accounts_svc.get_tx(session, account_id, tx_id)


@router.get("/accounts", response_class=HTMLResponse)
async def list_accounts(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "accounts.html",
        {"summaries": accounts_svc.all_summaries(session)},
    )


@router.get("/partials/add-account-form", response_class=HTMLResponse)
async def add_account_form(request: Request):
    return templates.TemplateResponse(request, "partials/add_account_form.html", {})


@router.post("/accounts", response_class=HTMLResponse)
async def create_account(
    request: Request,
    name: str = Form(...),
    label_kind: str = Form(""),
    owner: str = Form(""),
    note: str = Form(""),
    disposal_priority: str = Form("none"),
    session: Session = Depends(get_session),
):
    name = name.strip()
    if name:
        existing = {a.name for a in accounts_svc.list_accounts(session)}
        if name not in existing:
            accounts_svc.create_account(session, name=name, label_kind=label_kind, note=note,
                                        owner=owner, disposal_priority=disposal_priority)
    # Return the refreshed accounts grid (HTMX swaps it in).
    return templates.TemplateResponse(
        request, "partials/accounts_grid.html",
        {"summaries": accounts_svc.all_summaries(session)},
    )


@router.get("/accounts/{account_id}", response_class=HTMLResponse)
async def account_detail(account_id: int, request: Request, session: Session = Depends(get_session)):
    account = _account(session, account_id, request)
    if account is None:
        return RedirectResponse("/accounts", status_code=303)
    summary = accounts_svc.summarize(session, account)
    txs = tx_svc.list_transactions(session, account_id)
    wallets = accounts_svc.list_wallets(session, account_id)
    # Compute the internal-transfer set + node config ONCE and reuse (the breakdown and the tx
    # table both need internal_txids; the config was fetched twice).
    internal = costbasis.internal_txids(session)
    cfg = node_settings.get_config(session)
    # One ledger load for the account result + every per-wallet result (no N+1 recompute).
    cb, per_wallet_raw = costbasis.compute_account_breakdown(session, account_id, internal=internal)
    label_by_wallet = {w.id: w.label for w in wallets}
    per_wallet = [
        (label_by_wallet.get(wid, "Unassigned") if wid is not None else "Unassigned", res)
        for wid, res in per_wallet_raw
    ]
    return templates.TemplateResponse(
        request, "account_detail.html",
        {"account": account, "summary": summary, "txs": txs, "kinds": TxKind.ALL,
         "labels": TxKind.LABELS, "wallets": wallets,
         "electrum_host": cfg.electrum_host, "mempool_url": cfg.mempool_url,
         "cb": cb, "per_wallet": per_wallet, "network_enabled": config.ENABLE_NETWORK,
         "internal_txids": internal},
    )


@router.get("/accounts/{account_id}/audit", response_class=HTMLResponse)
async def account_audit(account_id: int, request: Request, session: Session = Depends(get_session)):
    account = _account(session, account_id, request)
    if account is None:
        return RedirectResponse("/accounts", status_code=303)
    return templates.TemplateResponse(
        request, "audit.html",
        {"account": account, "cb": costbasis.compute_account(session, account_id)},
    )


@router.get("/accounts/{account_id}/coins", response_class=HTMLResponse)
async def account_coins(account_id: int, request: Request, session: Session = Depends(get_session)):
    """UTXO inventory + privacy analysis for the account's on-chain coins."""
    account = _account(session, account_id, request)
    if account is None:
        return RedirectResponse("/accounts", status_code=303)
    utxos = coins_svc.list_utxos(session, account_id, unspent_only=True)
    return templates.TemplateResponse(
        request, "coins.html",
        {"account": account, "utxos": utxos,
         "total_sats": coins_svc.unspent_total_sats(utxos),
         "warnings": coins_svc.privacy_warnings(session, account_id),
         "mempool_url": node_settings.get_config(session).mempool_url},
    )


@router.get("/accounts/{account_id}/name", response_class=HTMLResponse)
async def account_name(account_id: int, request: Request, session: Session = Depends(get_session)):
    account = _account(session, account_id, request)
    if account is None:
        return _DENIED_PARTIAL
    return templates.TemplateResponse(request, "partials/account_name.html", {"account": account})


@router.get("/accounts/{account_id}/edit-form", response_class=HTMLResponse)
async def edit_form(account_id: int, request: Request, session: Session = Depends(get_session)):
    account = _account(session, account_id, request)
    if account is None:
        return _DENIED_PARTIAL
    return templates.TemplateResponse(request, "partials/account_edit_form.html", {"account": account, "error": ""})


@router.post("/accounts/{account_id}/edit", response_class=HTMLResponse)
async def edit_account(account_id: int, request: Request, name: str = Form(...),
                       label_kind: str = Form(""), owner: str = Form(""), note: str = Form(""),
                       lot_method: str = Form(""), disposal_priority: str = Form(""),
                       session: Session = Depends(get_session)):
    if _account(session, account_id, request) is None:
        return _DENIED_PARTIAL
    account, error = accounts_svc.update_account(session, account_id, name, label_kind, note, owner,
                                                 lot_method, disposal_priority)
    if error:
        return templates.TemplateResponse(request, "partials/account_edit_form.html",
                                          {"account": account, "error": error})
    return templates.TemplateResponse(request, "partials/account_name.html", {"account": account})


def _wallets_partial(request: Request, session: Session, account_id: int,
                     editing_id: int | None = None, edit_error: str = ""):
    return templates.TemplateResponse(
        request, "partials/wallets.html",
        {"wallets": accounts_svc.list_wallets(session, account_id),
         "account": accounts_svc.get_account(session, account_id),
         "electrum_host": node_settings.get_config(session).electrum_host,
         "editing_id": editing_id, "edit_error": edit_error},
    )


def _wallets_and_txtable(request: Request, session: Session, account_id: int, **tx_extra):
    """Render the wallets panel AND an out-of-band refresh of the transaction table in one
    response — so adding/syncing a wallet updates both at once (the form targets #wallets;
    the tx-table swaps via hx-swap-oob)."""
    wallets = _wallets_partial(request, session, account_id)
    txtable = _tx_table(request, session, account_id, tx_oob=True, **tx_extra)
    return HTMLResponse(wallets.body + b"\n" + txtable.body)


@router.get("/accounts/{account_id}/wallets", response_class=HTMLResponse)
async def wallets_partial(account_id: int, request: Request, session: Session = Depends(get_session)):
    if _account(session, account_id, request) is None:
        return _DENIED_PARTIAL
    return _wallets_partial(request, session, account_id)


@router.get("/wallets/{wallet_id}/edit-form", response_class=HTMLResponse)
async def wallet_edit_form(wallet_id: int, request: Request, session: Session = Depends(get_session)):
    w = _wallet(session, wallet_id, request)
    if w is None:
        return _DENIED_PARTIAL
    return _wallets_partial(request, session, w.account_id, editing_id=wallet_id)


@router.post("/wallets/{wallet_id}/edit", response_class=HTMLResponse)
async def wallet_edit(wallet_id: int, request: Request, label: str = Form(...),
                      xpub: str = Form(""), gap_limit: int = Form(20),
                      onchain_mode: str = Form("standalone"), address_type: str = Form("auto"),
                      session: Session = Depends(get_session)):
    w = _wallet(session, wallet_id, request)
    if w is None:
        return _DENIED_PARTIAL
    account_id = w.account_id
    _, error = accounts_svc.update_wallet(session, wallet_id, label, xpub, gap_limit,
                                          onchain_mode=onchain_mode, address_type=address_type)
    return _wallets_partial(request, session, account_id,
                            editing_id=wallet_id if error else None, edit_error=error)


@router.post("/wallets/{wallet_id}/delete", response_class=HTMLResponse)
async def wallet_delete(wallet_id: int, request: Request, session: Session = Depends(get_session)):
    if _wallet(session, wallet_id, request) is None:
        return _DENIED_PARTIAL
    account_id = accounts_svc.delete_wallet(session, wallet_id)
    if account_id is None:
        return _DENIED_PARTIAL
    return _wallets_partial(request, session, account_id)


@router.post("/accounts/{account_id}/wallets", response_class=HTMLResponse)
async def add_wallet(
    account_id: int,
    request: Request,
    label: str = Form(...),
    xpub: str = Form(""),
    gap_limit: int = Form(20),
    onchain_mode: str = Form("standalone"),
    address_type: str = Form("auto"),
    session: Session = Depends(get_session),
):
    if _account(session, account_id, request) is None:
        return _DENIED_PARTIAL
    xpub = xpub.strip()
    # Accept a single-sig xpub OR a multisig output descriptor; surface a clear error early.
    if xpub:
        err = accounts_svc.validate_key_or_descriptor(xpub)
        if err:
            return _wallets_partial(request, session, account_id, edit_error=err)
    mode = onchain_mode if onchain_mode in ("standalone", "custodial_fed") else "standalone"
    atype = address_type if address_type in ("auto", "p2wpkh", "p2sh-p2wpkh", "p2pkh") else "auto"
    wallet = accounts_svc.add_wallet(
        session, account_id=account_id, label=label, wtype=WalletType.XPUB,
        xpub=xpub or None, script_type="", gap_limit=gap_limit, onchain_mode=mode, address_type=atype,
    )
    # Auto-sync the new wallet so its transactions appear immediately (no separate Sync click).
    # Tor scans take a few seconds; any failure surfaces as a banner in the tx table.
    result = None
    if wallet.xpub:
        result = sync_svc.sync_wallet(session, wallet.id)
        costbasis.reconcile_internal_transfers(session)  # detect cross-wallet transfers
    extra = {"import_result": result, "import_source": "xpub sync"} if result is not None else {}
    return _wallets_and_txtable(request, session, account_id, **extra)


@router.post("/wallets/{wallet_id}/sync", response_class=HTMLResponse)
async def sync_wallet_route(wallet_id: int, request: Request, session: Session = Depends(get_session)):
    wallet = _wallet(session, wallet_id, request)
    if wallet is None:
        return _DENIED_PARTIAL
    account_id = wallet.account_id
    result = sync_svc.sync_wallet(session, wallet_id)
    account = accounts_svc.get_account(session, account_id)
    txs = tx_svc.list_transactions(session, account_id)
    return templates.TemplateResponse(
        request, "partials/tx_table.html",
        {"txs": txs, "account": account, "summary": accounts_svc.summarize(session, account),
         "labels": TxKind.LABELS, "import_result": result, "import_source": "xpub sync",
         "internal_txids": costbasis.internal_txids(session),
         "mempool_url": node_settings.get_config(session).mempool_url},
    )


@router.post("/accounts/{account_id}/sync-all", response_class=HTMLResponse)
async def sync_all(account_id: int, request: Request, session: Session = Depends(get_session)):
    """Sync every xpub wallet in the account, then reconcile cross-wallet transfers."""
    if _account(session, account_id, request) is None:
        return _DENIED_PARTIAL
    synced = imported = 0
    errors: list[str] = []
    for w in accounts_svc.list_wallets(session, account_id):
        if w.wtype != WalletType.XPUB or not w.xpub:
            continue
        r = sync_svc.sync_wallet(session, w.id)
        synced += 1
        imported += r.imported
        errors.extend(f"{w.label}: {e}" for e in r.errors)
    costbasis.reconcile_internal_transfers(session)  # cross-wallet transfer detection + basis carry
    summary = csv_import.ImportResult(imported=imported, errors=errors)
    src = f"sync all ({synced} wallet{'s' if synced != 1 else ''})"
    return _tx_table(request, session, account_id, import_result=summary, import_source=src)


@router.post("/accounts/{account_id}/delete")
async def delete_account(account_id: int, request: Request, session: Session = Depends(get_session)):
    if _account(session, account_id, request) is None:
        return RedirectResponse("/accounts", status_code=303)
    accounts_svc.delete_account(session, account_id)
    return RedirectResponse("/accounts", status_code=303)


@router.post("/accounts/{account_id}/transactions", response_class=HTMLResponse)
async def add_transaction(
    account_id: int,
    request: Request,
    kind: str = Form(...),
    timestamp: str = Form(""),
    amount_btc: str = Form("0"),
    fiat_value: str = Form(""),
    fee_btc: str = Form("0"),
    counterparty: str = Form(""),
    note: str = Form(""),
    session: Session = Depends(get_session),
):
    if _account(session, account_id, request) is None:
        return _DENIED_PARTIAL
    tx_svc.add_transaction(
        session,
        account_id=account_id,
        kind=kind,
        timestamp=tx_svc.parse_timestamp(timestamp),
        amount_sats=tx_svc.btc_to_sats(amount_btc),
        fee_sats=tx_svc.btc_to_sats(fee_btc),
        fiat_value=tx_svc.parse_usd(fiat_value),
        fiat_source="manual",  # a value the user typed in is authoritative
        counterparty=counterparty,
        note=note,
        source="manual",
    )
    return _tx_table(request, session, account_id)


@router.post("/accounts/{account_id}/import/csv", response_class=HTMLResponse)
async def import_csv_route(
    account_id: int,
    request: Request,
    source: str = Form("generic"),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    if _account(session, account_id, request) is None:
        return _DENIED_PARTIAL
    # Read with a hard ceiling so an oversized upload can't OOM the process.
    raw = await file.read(_MAX_CSV_BYTES + 1)
    if len(raw) > _MAX_CSV_BYTES:
        result = csv_import.ImportResult(
            errors=[f"File too large (max {_MAX_CSV_BYTES // (1024 * 1024)} MB)."])
        return _tx_table(request, session, account_id, import_result=result, import_source=source)
    text = raw.decode("utf-8-sig", errors="replace")
    result = csv_import.import_csv(session, account_id=account_id, source=source, text=text)
    # Imported rows default to buy/sell; connect any that share a txid with one of your own
    # wallets (e.g. a CSV withdrawal that landed in a loaded xpub) into transfers + carry basis.
    costbasis.reconcile_internal_transfers(session)
    return _tx_table(request, session, account_id, import_result=result, import_source=source)


@router.post("/accounts/{account_id}/transactions/{tx_id}/delete", response_class=HTMLResponse)
async def delete_transaction(
    account_id: int, tx_id: int, request: Request, session: Session = Depends(get_session)
):
    if _tx(session, account_id, tx_id, request) is None:
        return _DENIED_PARTIAL
    tx_svc.delete_transaction(session, tx_id)
    return _tx_table(request, session, account_id)


def _cost_basis_partial(request: Request, session: Session, account_id: int, oob: bool = False):
    """Render the cost-basis tile. With oob=True it carries hx-swap-oob so a tx-table refresh
    also updates the tile — otherwise the tile (holdings/basis/realized) goes stale after a
    sync/import/edit until a manual page reload."""
    cb, per_wallet_raw = costbasis.compute_account_breakdown(session, account_id)
    label_by_wallet = {w.id: w.label for w in accounts_svc.list_wallets(session, account_id)}
    per_wallet = [
        (label_by_wallet.get(wid, "Unassigned") if wid is not None else "Unassigned", res)
        for wid, res in per_wallet_raw
    ]
    return templates.TemplateResponse(
        request, "partials/cost_basis.html",
        {"account": accounts_svc.get_account(session, account_id),
         "cb": cb, "per_wallet": per_wallet, "cb_oob": oob},
    )


def _tx_table(request: Request, session: Session, account_id: int, **extra):
    account = accounts_svc.get_account(session, account_id)
    ctx = {"txs": tx_svc.list_transactions(session, account_id), "account": account,
           "summary": accounts_svc.summarize(session, account), "labels": TxKind.LABELS,
           "kinds": TxKind.ALL, "internal_txids": costbasis.internal_txids(session),
           "mempool_url": node_settings.get_config(session).mempool_url}
    ctx.update(extra)
    txtable = templates.TemplateResponse(request, "partials/tx_table.html", ctx)
    # Append an out-of-band refresh of the cost-basis tile so it never goes stale after a data
    # change (sync, import, add/edit/delete). HTMX applies hx-swap-oob elements anywhere in the
    # response body, independent of the primary swap target.
    cbtile = _cost_basis_partial(request, session, account_id, oob=True)
    return HTMLResponse(txtable.body + b"\n" + cbtile.body)


@router.get("/accounts/{account_id}/transactions", response_class=HTMLResponse)
async def tx_table_refresh(account_id: int, request: Request, session: Session = Depends(get_session)):
    if _account(session, account_id, request) is None:
        return _DENIED_PARTIAL
    return _tx_table(request, session, account_id)


@router.get("/accounts/{account_id}/transactions/{tx_id}/edit-form", response_class=HTMLResponse)
async def tx_edit_form(account_id: int, tx_id: int, request: Request, session: Session = Depends(get_session)):
    if _tx(session, account_id, tx_id, request) is None:
        return _DENIED_PARTIAL
    return _tx_table(request, session, account_id, editing_tx_id=tx_id)


@router.post("/accounts/{account_id}/transactions/{tx_id}/edit", response_class=HTMLResponse)
async def tx_edit(account_id: int, tx_id: int, request: Request,
                  kind: str = Form(...), timestamp: str = Form(""), amount_btc: str = Form("0"),
                  fiat_value: str = Form(""), counterparty: str = Form(""), note: str = Form(""),
                  txid: str = Form(""), address: str = Form(""), acquired_at: str = Form(""),
                  cost_basis: str = Form(""), session: Session = Depends(get_session)):
    if _tx(session, account_id, tx_id, request) is None:
        return _DENIED_PARTIAL
    acq = tx_svc.parse_timestamp(acquired_at) if acquired_at.strip() else None
    tx_svc.update_transaction(
        session, tx_id, kind=kind, timestamp=tx_svc.parse_timestamp(timestamp),
        amount_sats=tx_svc.btc_to_sats(amount_btc), fiat_value=tx_svc.parse_usd(fiat_value),
        counterparty=counterparty, note=note,
        txid=txid, address=address, acquired_at=acq,
        carried_basis_usd=tx_svc.parse_usd(cost_basis), set_links=True,
    )
    # Adding/correcting a txid or destination address can link this to one of your wallets — run
    # the reconciler so a now-provable self-transfer auto-relabels + carries basis/KYC.
    if txid.strip() or address.strip():
        costbasis.reconcile_internal_transfers(session)
    return _tx_table(request, session, account_id)


@router.post("/accounts/{account_id}/prices/fetch", response_class=HTMLResponse)
async def fetch_prices(account_id: int, request: Request, session: Session = Depends(get_session)):
    if _account(session, account_id, request) is None:
        return _DENIED_PARTIAL
    from app.services import pricing
    result = pricing.backfill_prices(session, account_id)
    return _tx_table(request, session, account_id, price_result=result)


@router.post("/accounts/{account_id}/reconcile")
async def reconcile_transfers(account_id: int, request: Request, session: Session = Depends(get_session)):
    if _account(session, account_id, request) is None:
        return RedirectResponse("/accounts", status_code=303)
    # Carries basis across cross-account self-transfers, then reloads this account.
    costbasis.reconcile_internal_transfers(session)
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@router.post("/accounts/{account_id}/transactions/{tx_id}/carry-toggle", response_class=HTMLResponse)
async def carry_toggle(account_id: int, tx_id: int, request: Request, session: Session = Depends(get_session)):
    tx = _tx(session, account_id, tx_id, request)
    if tx is None:
        return _DENIED_PARTIAL
    if tx.kind == TxKind.TRANSFER_IN:
        tx.carry_disabled = not tx.carry_disabled
        if tx.carry_disabled:
            tx.carried_basis_usd = None  # opt out -> fresh basis
            tx.carried_lots = None       # and drop the carried lot fragments
        session.commit()
        if not tx.carry_disabled:
            costbasis.reconcile_internal_transfers(session)  # opt back in -> re-apply carry
    return _tx_table(request, session, account_id)


@router.get("/accounts/{account_id}/transactions/{tx_id}/gift-statement", response_class=HTMLResponse)
async def gift_statement(account_id: int, tx_id: int, request: Request, donor: str = "",
                         recipient: str = "", gift_tax: str = "", session: Session = Depends(get_session)):
    account = _account(session, account_id, request)
    tx = _tx(session, account_id, tx_id, request)
    if account is None or tx is None:
        return RedirectResponse("/accounts", status_code=303)
    cb = costbasis.compute_account(session, account_id)
    key = costbasis.tx_key(tx)
    carryover = cb.transfer_out_basis.get(key) if key else None
    lots = cb.transfer_out_lots.get(key, []) if key else []
    acquisition_dates = sorted({lot["acquired"].date() for lot in lots})
    fmv = None
    if tx.price_usd is not None:
        fmv = (tx.price_usd * tx.amount_btc).quantize(Decimal("0.01"))
    elif tx.fiat_value is not None:
        fmv = tx.fiat_value
    annual_exclusion, exclusion_known = _gift_exclusion_for(tx.timestamp.year)
    return templates.TemplateResponse(
        request, "gift_statement.html",
        {"account": account, "tx": tx, "carryover_basis": carryover, "fmv": fmv,
         "acquisition_dates": acquisition_dates, "donor": donor or account.owner,
         "recipient": recipient, "gift_tax": gift_tax, "annual_exclusion": annual_exclusion,
         "exclusion_known": exclusion_known,
         "over_709": fmv is not None and fmv > annual_exclusion},
    )
