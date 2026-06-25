# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Settings → Node + local AI connections (Sparrow-style node config; user-defined LLMs)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import LLMConnection
from app.services import llm, node_settings, outbound
from app.templating import templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request, "settings.html",
        {"cfg": (_cfg := node_settings.get_config(session)), "saved": False, "result": None,
         "explorer_is_public": not node_settings.explorer_is_private(_cfg.mempool_url),
         "outbound": outbound.recent(session), "llm_conns": llm.list_connections(session)},
    )


# --- Local AI connections ----------------------------------------------------
def _llm_section(request: Request, session: Session, **extra):
    ctx = {"llm_conns": llm.list_connections(session)}
    ctx.update(extra)
    return templates.TemplateResponse(request, "partials/llm_connections.html", ctx)


def _transient_conn(provider: str, base_url: str, model: str, api_key: str = ""):
    return LLMConnection(name="(test)", provider=(provider or "ollama").strip(),
                         base_url=(base_url or "").strip().rstrip("/"), model=(model or "").strip(),
                         api_key=(api_key or "").strip())


@router.post("/settings/llm/add", response_class=HTMLResponse)
async def llm_add(request: Request, name: str = Form(""), provider: str = Form("ollama"),
                  base_url: str = Form(""), model: str = Form(""),
                  session: Session = Depends(get_session)):
    if base_url.strip():
        llm.add_connection(session, name=name, provider=provider, base_url=base_url,
                           model=model)
    return _llm_section(request, session)


@router.get("/settings/llm/{conn_id}/edit-form", response_class=HTMLResponse)
async def llm_edit_form(conn_id: int, request: Request, session: Session = Depends(get_session)):
    return _llm_section(request, session, editing_id=conn_id)


@router.post("/settings/llm/{conn_id}/edit", response_class=HTMLResponse)
async def llm_edit(conn_id: int, request: Request, name: str = Form(""), provider: str = Form("ollama"),
                   base_url: str = Form(""), model: str = Form(""),
                   session: Session = Depends(get_session)):
    llm.update_connection(session, conn_id, name=name, provider=provider, base_url=base_url,
                          model=model)
    return _llm_section(request, session)


@router.post("/settings/llm/{conn_id}/default", response_class=HTMLResponse)
async def llm_default(conn_id: int, request: Request, session: Session = Depends(get_session)):
    llm.set_default(session, conn_id)
    return _llm_section(request, session)


@router.post("/settings/llm/{conn_id}/delete", response_class=HTMLResponse)
async def llm_delete(conn_id: int, request: Request, session: Session = Depends(get_session)):
    llm.delete_connection(session, conn_id)
    return _llm_section(request, session)


@router.post("/settings/llm/test", response_class=HTMLResponse)
async def llm_test(request: Request, provider: str = Form("ollama"), base_url: str = Form(""),
                   model: str = Form(""),
                   session: Session = Depends(get_session)):
    result = llm.test_connection(_transient_conn(provider, base_url, model))
    return templates.TemplateResponse(request, "partials/llm_test.html", {"result": result})


@router.post("/settings/llm/models", response_class=HTMLResponse)
async def llm_models(request: Request, provider: str = Form("ollama"), base_url: str = Form(""),
                     session: Session = Depends(get_session)):
    models = llm.list_models(_transient_conn(provider, base_url, ""))
    # Escape: model names come from the (user-pointed-at) server and land in HTML attributes.
    return HTMLResponse("".join(f'<option value="{escape(m)}"></option>' for m in models))


@router.post("/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    electrum_host: str = Form(""),
    electrum_port: int = Form(50001),
    use_ssl: bool = Form(False),
    use_tor: bool = Form(False),
    tor_host: str = Form("127.0.0.1"),
    tor_port: int = Form(9050),
    mempool_url: str = Form(""),
    price_source: str = Form("coinbase"),
    session: Session = Depends(get_session),
):
    cfg = node_settings.save_config(
        session, electrum_host=electrum_host, electrum_port=electrum_port, use_ssl=use_ssl,
        use_tor=use_tor, tor_host=tor_host, tor_port=tor_port, mempool_url=mempool_url,
        price_source=price_source,
    )
    return templates.TemplateResponse(
        request, "settings.html",
        {"cfg": cfg, "saved": True, "result": None, "outbound": outbound.recent(session),
         "explorer_is_public": not node_settings.explorer_is_private(cfg.mempool_url),
         "llm_conns": llm.list_connections(session)},
    )


@router.post("/settings/test", response_class=HTMLResponse)
async def test_settings(
    request: Request,
    electrum_host: str = Form(""),
    electrum_port: int = Form(50001),
    use_ssl: bool = Form(False),
    use_tor: bool = Form(False),
    tor_host: str = Form("127.0.0.1"),
    tor_port: int = Form(9050),
    session: Session = Depends(get_session),
):
    if electrum_host.strip():
        outbound.record(electrum_host.strip(), "node connection test")
    result = node_settings.test_params(
        electrum_host=electrum_host, electrum_port=electrum_port, use_ssl=use_ssl,
        use_tor=use_tor, tor_host=tor_host, tor_port=tor_port,
    )
    return templates.TemplateResponse(request, "partials/node_status.html", {"result": result})


@router.post("/settings/outbound/clear")
async def clear_outbound(request: Request, session: Session = Depends(get_session)):
    outbound.clear(session)
    return RedirectResponse("/settings", status_code=303)
