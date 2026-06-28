# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Local-only egress guard: with the network off (the default; conftest sets
BTT_ENABLE_NETWORK=0) and no node/LLM configured, an ordinary accounting workflow must record
ZERO outbound actions. Regression test for the privacy promise."""
import re

from app.db import SessionLocal
from app.services import outbound


def test_local_workflow_logs_no_outbound(client):
    # Representative local-only workflow: create an account, add buy/sell transactions, then
    # render the pages that compute cost basis and taxes.
    client.post("/accounts", data={"name": "EgressAcct"})
    aid = re.search(r"/accounts/(\d+)", client.get("/accounts").text).group(1)
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "buy", "timestamp": "2025-01-01", "amount_btc": "1.0", "fiat_value": "30000"})
    client.post(f"/accounts/{aid}/transactions",
                data={"kind": "sell", "timestamp": "2025-06-01", "amount_btc": "0.4", "fiat_value": "25000"})
    for path in ("/", "/accounts", f"/accounts/{aid}", f"/accounts/{aid}/audit", "/tax"):
        assert client.get(path).status_code == 200

    # Nothing should have been logged to the Outbound Data Log.
    with SessionLocal() as s:
        assert outbound.recent(s) == []
