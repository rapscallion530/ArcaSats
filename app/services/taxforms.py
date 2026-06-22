# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
"""US tax forms from cost-basis results.

Form 8949 (per disposal) split into Part I (short-term) / Part II (long-term),
Schedule D totals, and ordinary-income summary. Produces an 8949-format report
(not a filed IRS PDF) suitable for an accountant or transcription into tax software.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass
from decimal import Decimal

from app.models import SATS_PER_BTC, Transaction, TxKind
from app.services.costbasis import CostBasisResult

_CENTS = Decimal("0.01")


@dataclass
class Form8949Row:
    description: str
    acquired: dt.datetime
    sold: dt.datetime
    proceeds: Decimal
    basis: Decimal
    term: str

    @property
    def gain(self) -> Decimal:
        return (self.proceeds - self.basis).quantize(_CENTS)


def _btc(sats: int) -> str:
    return f"{Decimal(sats) / SATS_PER_BTC:.8f}"


def build_rows(result: CostBasisResult, year: int | None = None) -> list[Form8949Row]:
    rows = []
    for d in result.disposals:
        if year is not None and d.date.year != year:
            continue
        rows.append(Form8949Row(
            description=f"{_btc(d.sats)} BTC",
            acquired=d.acquired, sold=d.date,
            proceeds=d.proceeds_usd, basis=d.basis_usd, term=d.term,
        ))
    rows.sort(key=lambda r: (r.term, r.sold))
    return rows


def totals(rows: list[Form8949Row]) -> dict:
    def agg(term: str) -> dict:
        sel = [r for r in rows if r.term == term]
        return {
            "count": len(sel),
            "proceeds": sum((r.proceeds for r in sel), Decimal("0")).quantize(_CENTS),
            "basis": sum((r.basis for r in sel), Decimal("0")).quantize(_CENTS),
            "gain": sum((r.gain for r in sel), Decimal("0")).quantize(_CENTS),
        }
    short, long = agg("short"), agg("long")
    return {
        "short": short, "long": long,
        "net_gain": (short["gain"] + long["gain"]).quantize(_CENTS),
    }


def years_present(result: CostBasisResult) -> list[int]:
    return sorted({d.date.year for d in result.disposals})


def income_for_year(txs: list[Transaction], year: int) -> Decimal:
    total = Decimal("0")
    for t in txs:
        if t.kind == TxKind.INCOME and t.timestamp.year == year:
            total += (t.fiat_value or Decimal("0"))
    return total.quantize(_CENTS)


def to_csv(rows: list[Form8949Row], account_name: str, year: int | None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"Form 8949 — {account_name}" + (f" — {year}" if year else "")])
    w.writerow(["Part", "Description", "Date acquired", "Date sold", "Proceeds (USD)",
                "Cost basis (USD)", "Gain/loss (USD)"])
    for r in rows:
        part = "I (short-term)" if r.term == "short" else "II (long-term)"
        w.writerow([part, r.description, f"{r.acquired:%Y-%m-%d}", f"{r.sold:%Y-%m-%d}",
                    f"{r.proceeds:.2f}", f"{r.basis:.2f}", f"{r.gain:.2f}"])
    t = totals(rows)
    w.writerow([])
    w.writerow(["Schedule D — short-term total", "", "", "", f"{t['short']['proceeds']:.2f}",
                f"{t['short']['basis']:.2f}", f"{t['short']['gain']:.2f}"])
    w.writerow(["Schedule D — long-term total", "", "", "", f"{t['long']['proceeds']:.2f}",
                f"{t['long']['basis']:.2f}", f"{t['long']['gain']:.2f}"])
    w.writerow(["Net capital gain/loss", "", "", "", "", "", f"{t['net_gain']:.2f}"])
    return buf.getvalue()
