# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Assistant: ask read-only questions about your own data via a local LLM."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_session
from app.services import assistant as assistant_svc
from app.services import llm
from app.templating import templates

router = APIRouter()


@router.get("/assistant", response_class=HTMLResponse)
async def assistant_page(request: Request, session: Session = Depends(get_session)):
    conns = llm.list_connections(session)
    return templates.TemplateResponse(
        request, "assistant.html",
        {"conns": conns, "default": llm.get_default(session)},
    )


@router.post("/assistant/ask", response_class=HTMLResponse)
async def assistant_ask(request: Request, question: str = Form(""), conn_id: int = Form(0),
                        session: Session = Depends(get_session)):
    conn = llm.get_connection(session, conn_id) if conn_id else llm.get_default(session)
    if conn is None:
        result = llm.ChatResult(False, error="No local model is configured yet. Add one in "
                                "Settings → Local AI.")
    else:
        result = assistant_svc.ask(session, conn, question)
    return templates.TemplateResponse(
        request, "partials/assistant_answer.html",
        {"result": result, "question": question, "conn": conn},
    )
