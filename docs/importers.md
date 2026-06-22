# Importers

## CSV sources

Parsers live in `app/services/importers/csv_import.py`. Header matching is
case-insensitive and tolerant of common variants. Bitcoin-only: non-BTC assets
(e.g. Coinbase ETH rows) are ignored.

| Source | Status | Notes |
|---|---|---|
| `coinbase` | Implemented vs. documented Coinbase "Transaction history" headers | Filters `Asset == BTC`. Uses *Total (inclusive of fees)* as fiat value. |
| `strike` | Best-effort vs. plausible headers | **Verify against a real (redacted) Strike export.** CSV is the reliable path (API is payments-oriented). |
| `swan` | Best-effort vs. plausible headers | **Verify against a real Swan export.** Swan has no individual API → CSV only. |
| `bisq` | Best-effort vs. plausible headers | Bisq v1/v2 local CSV export; no remote API. |
| `generic` | Canonical format, always supported | Columns: `date,type,amount_btc,usd_value,fee_btc,txid,external_id,counterparty,note`. |

### ⚠️ Validate real headers
The Strike/Swan/Bisq mappings were written without a real export to hand. Before
trusting them, drop a **redacted** sample (headers + 1-2 fake rows) and the mapping
will be confirmed/adjusted. The `generic` format is a guaranteed fallback for any source.

### Kind mapping
`buy/purchase → buy`, `sell → sell`, `reward(s)/income/interest → income`,
`spend/payment → spend`, `deposit/receive → transfer_in`,
`withdrawal/send/withdraw → transfer_out`. Transfers move coins without realizing
gain; reclassify a transfer as `sell`/`spend` if it was actually a disposal.

### Dedupe
Each row gets `source = "csv:<name>"` and an `external_id` (from the file if present,
else a stable hash of timestamp+kind+amount+value+txid). A `UniqueConstraint(source,
external_id)` makes re-imports idempotent.

## xpub (on-chain) — Phase 3
See `app/services/importers/xpub.py` and `app/services/electrum.py`.
