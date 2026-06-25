# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""Read-only "Ask your data" assistant.

Builds a compact, structured snapshot from the DETERMINISTIC cost-basis engine and asks a
local LLM to interpret it. The model never computes tax figures and never touches the
database — every number in the snapshot comes from our own math, so answers are grounded.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SATS_PER_BTC, Transaction, TxKind
from app.services import accounts as accounts_svc
from app.services import costbasis, llm

_MAX_DISPOSALS = 250  # cap detail rows so the snapshot fits small local context windows


def _btc(sats: int) -> str:
    return f"{Decimal(int(sats)) / SATS_PER_BTC:.8f}"


def _money(value) -> float:
    return float(Decimal(value).quantize(Decimal("0.01")))


def build_snapshot(session: Session) -> dict:
    """A JSON-serializable summary of the portfolio + realized tax figures."""
    accounts = accounts_svc.list_accounts(session)

    acct_blocks, disposals_out = [], []
    totals = {"holdings_btc": 0, "holdings_basis_usd": Decimal("0"),
              "realized_short_usd": Decimal("0"), "realized_long_usd": Decimal("0"),
              "income_usd": Decimal("0")}
    years: set[int] = set()
    missing_price = 0

    for acct in accounts:
        cb = costbasis.compute_account(session, acct.id)
        by_year: dict[int, dict] = defaultdict(lambda: {"short_usd": Decimal("0"), "long_usd": Decimal("0"),
                                                         "proceeds_usd": Decimal("0"), "count": 0})
        for d in cb.disposals:
            y = d.date.year
            years.add(y)
            key = "short_usd" if d.term == "short" else "long_usd"
            by_year[y][key] += d.gain_usd
            by_year[y]["proceeds_usd"] += d.proceeds_usd
            by_year[y]["count"] += 1
            if len(disposals_out) < _MAX_DISPOSALS:
                disposals_out.append({
                    "account": acct.name, "date": d.date.strftime("%Y-%m-%d"), "kind": d.kind,
                    "btc": _btc(d.sats), "term": d.term, "proceeds_usd": _money(d.proceeds_usd),
                    "basis_usd": _money(d.basis_usd), "gain_usd": _money(d.gain_usd),
                    "acquired": d.acquired.strftime("%Y-%m-%d"),
                })

        acct_blocks.append({
            "name": acct.name,
            "owner": acct.owner or "(you)",
            "label": acct.label_kind or "",
            "lot_method": acct.lot_method.upper(),
            "holdings_btc": _btc(cb.holding_sats),
            "holdings_basis_usd": _money(cb.holding_basis_usd),
            "realized_short_usd": _money(cb.realized_short_usd),
            "realized_long_usd": _money(cb.realized_long_usd),
            "income_usd": _money(cb.income_usd),
            "by_year": {str(y): {"short_usd": _money(v["short_usd"]), "long_usd": _money(v["long_usd"]),
                                 "proceeds_usd": _money(v["proceeds_usd"]), "disposals": v["count"]}
                        for y, v in sorted(by_year.items())},
            "warnings": cb.warnings,
        })
        totals["holdings_btc"] += cb.holding_sats
        totals["holdings_basis_usd"] += cb.holding_basis_usd
        totals["realized_short_usd"] += cb.realized_short_usd
        totals["realized_long_usd"] += cb.realized_long_usd
        totals["income_usd"] += cb.income_usd

    # Data-quality flags the user may want to resolve before filing.
    acct_ids = [a.id for a in accounts]
    if acct_ids:
        for tx in session.scalars(select(Transaction).where(Transaction.account_id.in_(acct_ids))):
            if tx.kind in (TxKind.SELL, TxKind.SPEND, TxKind.BUY, TxKind.INCOME) and tx.fiat_value is None:
                missing_price += 1

    return {
        "generated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "currency": "USD unless a field says BTC",
        "accounts": acct_blocks,
        "totals": {
            "holdings_btc": _btc(totals["holdings_btc"]),
            "holdings_basis_usd": _money(totals["holdings_basis_usd"]),
            "realized_short_usd": _money(totals["realized_short_usd"]),
            "realized_long_usd": _money(totals["realized_long_usd"]),
            "income_usd": _money(totals["income_usd"]),
        },
        "years_with_disposals": sorted(years),
        "disposals": disposals_out,
        "disposals_truncated": len(disposals_out) >= _MAX_DISPOSALS,
        "data_quality": {"transactions_missing_usd_price": missing_price},
    }


_SYSTEM = (
    "You are the assistant inside ArcaSats, a local-only US Bitcoin tax & accounting app. "
    "Answer the user's question USING ONLY the JSON snapshot provided — it is the user's own "
    "Bitcoin data, already computed by the app's deterministic cost-basis engine. Rules: "
    "all amounts are USD unless a field name ends in _btc or says BTC; do not invent numbers or "
    "perform tax calculations beyond simple arithmetic on the given figures; if the snapshot "
    "doesn't contain the answer, say so plainly and suggest what to add (e.g. fetch USD prices, "
    "import a CSV, set an opening balance). Be concise and use the user's account names. "
    "End anything resembling tax guidance with a one-line reminder that this is not tax advice."
)


def build_messages(snapshot: dict, question: str) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": (
            "DATA SNAPSHOT (JSON):\n" + json.dumps(snapshot, ensure_ascii=False)
            + "\n\nQUESTION: " + question.strip())},
    ]


def ask(session: Session, conn, question: str) -> llm.ChatResult:
    if not question.strip():
        return llm.ChatResult(False, error="Type a question first.")
    snapshot = build_snapshot(session)
    return llm.chat(conn, build_messages(snapshot, question))
