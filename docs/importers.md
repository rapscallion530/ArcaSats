# Importers

## CSV sources

Parsers live in `app/services/importers/csv_import.py`. Header matching is
case-insensitive and tolerant of common variants. Bitcoin-only: non-BTC assets
(e.g. Coinbase ETH rows) are ignored.

| Source | Status | Notes |
|---|---|---|
| `coinbase` | Implemented vs. documented Coinbase "Transaction history" headers | Filters `Asset == BTC`. Uses *Total (inclusive of fees)* as fiat value. |
| `strike` | Validated against a real (sanitized) Annual Account Statement | Handles month-name dates, USD-only fiat/Lightning rows, pending rows, and on-chain Send hashes. CSV is the reliable path (API is payments-oriented). |
| `swan` | Validated against real (sanitized) exports | Handles **both** Swan exports under one `swan` source — auto-detected. Swan has no individual API → CSV only. |
| `bisq` | Best-effort vs. plausible headers | Bisq v1/v2 local CSV export; no remote API. |
| `generic` | Canonical format, always supported | Columns: `date,type,amount_btc,usd_value,fee_btc,txid,external_id,counterparty,note`. |

### Swan exports (two formats, one source)
Swan ships two unrelated CSVs, both prefixed with a company/phone **banner** the importer
skips automatically (see `_strip_preamble`):
- **Transactions** (`Event, Date, …, Unit Count, Asset Type, BTC Price, …`): `purchase` → buy,
  BTC `deposit` → transfer-in. Non-BTC rows (USD funding deposits, `monthly_fee`) are filtered
  out by `Asset Type`. BTC amount comes from `Unit Count`, fiat from `Transaction USD`.
- **Withdrawals** (`Created At, Transaction ID, Executed At, …, Status, Bitcoin Amount, …`): has
  no `Event` column — every `settled` row is a transfer-out; `*-canceled` rows are dropped. The
  `Transaction ID` is the **on-chain txid**, stored on the transaction.

**Reconciliation:** because the withdrawal's on-chain txid is captured, once you sync the
receiving self-custody wallet (xpub) its matching `transfer_in` carries the same txid, and
`costbasis.reconcile_internal_transfers` recognizes the pair as an internal self-transfer
(same owner) and carries cost basis across — no manual linking needed.

### Strike (Annual Account Statement)
Header: `Transaction ID, Time (UTC), Status, Transaction Type, Amount USD, Fee USD, Amount BTC,
Fee BTC, Description, Exchange Rate, Transaction Hash`. `Purchase` → buy, `Send` → transfer-out;
amount from `Amount BTC`, fiat from `Amount USD`, price from `Exchange Rate`, on-chain txid from
`Transaction Hash`. **Only BTC rows are kept.** Strike is a dual USD+BTC account: a row with no
`Amount BTC` is USD-account activity — bank `Deposit`/`Withdrawal`, or a USD `Send` that Strike
instantly converts to BTC to settle a Lightning/on-chain invoice (BTC acquired + spent in the
same instant ⇒ never held ⇒ no disposal of held BTC, ~zero gain). Skipping these is the *correct*
tax treatment, not a data workaround — do **not** derive a BTC size from the USD, as that would
fabricate disposals that never happened.

**BTC rows default to a taxable buy/sell, never a transfer** (`_STRIKE_KIND`): `Purchase`/
`Receive` → buy, `Sale`/`Send` → sell. This is the conservative treatment — BTC leaving to /
arriving from an unknown destination is a disposal/acquisition until the user downgrades it to a
transfer by connecting it to one of their own wallets (reconciliation inbox / shared txid).
**Bill pay** (2025+: pay a USD bill with BTC) is a `Sale` (the BTC disposal — kept, proceeds in
`Amount USD`) paired with a `Withdrawal` (the USD to the biller — skipped), sharing one
Transaction ID; only the Sale survives, so there's no dedup collision. Non-`Completed` rows
(Pending/Reversed) are skipped. Dates are month-name (`Oct 10 2022 22:41:09`), parsed by `_dt`.

### ⚠️ Validate real headers
The Bisq mapping was written without a real export to hand. Before trusting it, drop a
**redacted** sample (headers + 1-2 fake rows) and the mapping will be confirmed/adjusted. The
`generic` format is a guaranteed fallback for any source.

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
