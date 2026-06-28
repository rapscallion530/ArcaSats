# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Settings → Node + local AI connections (Sparrow-style node config; user-defined LLMs)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import LLMConnection
from app.services import llm, node_settings, outbound, pricing
from app.templating import templates

router = APIRouter()


def _page_ctx(session: Session, **extra):
    cfg = node_settings.get_config(session)
    ctx = {"cfg": cfg, "saved": False, "result": None, "mempool_result": None,
           "explorer_is_public": not node_settings.explorer_is_private(cfg.mempool_url),
           "outbound": outbound.recent(session), "llm_conns": llm.list_connections(session),
           "price_sources": pricing.price_source_choices()}
    ctx.update(extra)
    return ctx


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(request, "settings.html", _page_ctx(session))


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
                     datalist_id: str = Form("models-add"),
                     session: Session = Depends(get_session)):
    models = llm.list_models(_transient_conn(provider, base_url, ""))
    # Escape: model names + the URL come from the (user-pointed-at) server and land in HTML.
    opts = "".join(f'<option value="{escape(m)}"></option>' for m in models)
    dl_id = escape(datalist_id or "models-add")
    if models:
        msg = (f'<span class="text-gain">Found {len(models)} model(s)</span> — '
               "click the Model field to pick one.")
    elif not llm.assistant_endpoint_allowed(base_url):
        msg = ('<span class="text-warn">That endpoint isn\'t on this machine.</span> '
               "The assistant only talks to a loopback address (e.g. http://127.0.0.1:11434); "
               "set BTT_ASSISTANT_ALLOW_LAN=1 to allow a model elsewhere on your LAN.")
    else:
        msg = (f'<span class="text-warn">No models found at {escape(base_url)}.</span> '
               "Is the model server running there?")
    # The message swaps into the visible status span (the button's target); the datalist is
    # refreshed out-of-band so the Model input's autocomplete picks up the new options.
    return HTMLResponse(f'{msg}<datalist id="{dl_id}" hx-swap-oob="true">{opts}</datalist>')


@router.post("/settings", response_class=HTMLResponse)
async def save_node_settings(
    request: Request,
    electrum_host: str = Form(""),
    electrum_port: int = Form(50001),
    use_ssl: bool = Form(False),
    use_tor: bool = Form(False),
    tor_host: str = Form("127.0.0.1"),
    tor_port: int = Form(9050),
    session: Session = Depends(get_session),
):
    node_settings.save_node(
        session, electrum_host=electrum_host, electrum_port=electrum_port, use_ssl=use_ssl,
        use_tor=use_tor, tor_host=tor_host, tor_port=tor_port,
    )
    return templates.TemplateResponse(request, "settings.html", _page_ctx(session, saved=True))


@router.post("/settings/mempool", response_class=HTMLResponse)
async def save_mempool_settings(
    request: Request,
    mempool_url: str = Form(""),
    mempool_use_tor: bool = Form(False),
    price_source: str = Form("coinbase"),
    session: Session = Depends(get_session),
):
    node_settings.save_mempool(
        session, mempool_url=mempool_url, mempool_use_tor=mempool_use_tor, price_source=price_source,
    )
    return templates.TemplateResponse(request, "settings.html", _page_ctx(session, saved=True))


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


@router.post("/settings/mempool/test", response_class=HTMLResponse)
async def test_mempool_settings(
    request: Request,
    mempool_url: str = Form(""),
    mempool_use_tor: bool = Form(False),
    session: Session = Depends(get_session),
):
    # The Tor SOCKS proxy is saved with the node; reuse it for the mempool test.
    cfg = node_settings.get_config(session)
    if mempool_url.strip():
        from urllib.parse import urlparse
        host = urlparse(mempool_url.strip()).hostname or mempool_url.strip()
        outbound.record(host, "mempool connection test")
    result = node_settings.test_mempool_params(
        mempool_url=mempool_url, mempool_use_tor=mempool_use_tor,
        tor_host=cfg.tor_host, tor_port=cfg.tor_port,
    )
    return templates.TemplateResponse(request, "partials/mempool_status.html",
                                      {"mempool_result": result})


@router.post("/settings/outbound/clear")
async def clear_outbound(request: Request, session: Session = Depends(get_session)):
    outbound.clear(session)
    return RedirectResponse("/settings", status_code=303)
