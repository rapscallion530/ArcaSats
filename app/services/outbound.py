# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Outbound Data Log — records intentional network actions locally for transparency.

Only host + purpose (+ a short non-sensitive detail) are stored — never addresses,
xpubs, txids, amounts, or any coin/user data. Logging never breaks a request.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import OutboundLog


def record(host: str, purpose: str, detail: str = "") -> None:
    try:
        with SessionLocal() as s:
            s.add(OutboundLog(host=host[:255], purpose=purpose[:120], detail=detail[:255]))
            s.commit()
    except Exception:  # never let logging break the actual operation
        pass


def recent(session: Session, limit: int = 50) -> list[OutboundLog]:
    return list(session.scalars(select(OutboundLog).order_by(OutboundLog.id.desc()).limit(limit)))


def clear(session: Session) -> None:
    session.query(OutboundLog).delete()
    session.commit()
