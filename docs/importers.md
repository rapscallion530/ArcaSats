# Importers

## CSV sources

Parsers live in `app/services/importers/csv_import.py`. Header matching is
case-insensitive and tolerant of common variants. Bitcoin-only: non-BTC assets
(e.g. Coinbase ETH rows) are ignored.

### App-wide rule: default to buy/sell, never transfers
A BTC movement defaults to a **taxable buy/sell** — coins leaving to (or arriving from) an
unknown destination are a disposal/acquisition. A row only becomes a non-taxable **transfer**
when it can be *connected* to another of your own wallets: an on-chain txid shared with a
same-owner wallet auto-upgrades both sides (`costbasis.reclassify_onchain_transfers`, run by
the reconciler on sync/import), or you confirm it in the reconciliation inbox. This is the
conservative treatment (it never hides a disposal) and matches standalone xpub mode. Custodial
exports (`_CUSTODIAL_KIND`) therefore never emit transfers; only the user-controlled `generic`
format honors an **explicit** `transfer_in`/`transfer_out` (you asserting the self-transfer).

| Source | Status | Notes |
|---|---|---|
| `coinbase` | Validated against a real (sanitized) "Transaction history" export | Skips the 3-line preamble; `" UTC"` dates; `Convert`/Pro rows resolved by quantity sign; `($x)` negatives; fee-inclusive `Total` as basis/proceeds. Filters `Asset == BTC`. |
| `strike` | Validated against a real (sanitized) Annual Account Statement | Handles month-name dates, USD-only fiat/Lightning rows, pending rows, and on-chain Send hashes. CSV is the reliable path (API is payments-oriented). |
| `swan` | Validated against real (sanitized) exports | Handles **both** Swan exports under one `swan` source — auto-detected. Swan has no individual API → CSV only. |
| `bisq` | Best-effort vs. plausible headers | Bisq v1/v2 local CSV export; no remote API. |
| `generic` | Canonical format, always supported | Columns: `date,type,amount_btc,usd_value,fee_btc,txid,external_id,counterparty,note`. |

### Swan exports (two formats, one source)
Swan ships two unrelated CSVs, both prefixed with a company/phone **banner** the importer
skips automatically (see `_strip_preamble`):
- **Transactions** (`Event, Date, …, Unit Count, Asset Type, BTC Price, …`): `purchase`/BTC
  `deposit` → **buy** (per the app-wide default). Non-BTC rows (USD funding deposits,
  `monthly_fee`) are filtered out by `Asset Type`. BTC amount comes from `Unit Count`, fiat from
  `Transaction USD`.
- **Withdrawals** (`Created At, Transaction ID, Executed At, …, Status, Bitcoin Amount, …`): has
  no `Event` column — every `settled` row is a **sell** by default; `*-canceled` rows are dropped.
  The `Transaction ID` is the **on-chain txid**, stored on the transaction.

**Reconciliation:** because the withdrawal's on-chain txid is captured, once you load the
receiving self-custody wallet (its xpub receive imports as a buy with the same txid), the
reconciler connects the two, upgrades both to a transfer, and carries cost basis across — no
manual linking needed.

### Coinbase (Transaction history)
Header (after a 3-line `Transactions` / `User,<name>,<id>` preamble that `_strip_preamble`
skips): `ID, Timestamp, Transaction Type, Asset, Quantity Transacted, Price Currency, Price at
Transaction, Subtotal, Total (inclusive of fees and/or spread), Fees and/or Spread, Notes, ...`.
- Dates carry a `" UTC"` suffix (handled by `_dt`); negatives are accounting-style `"($84.63)"`
  (handled by `_usd`).
- `Buy`→buy, `Send`→sell. **`Convert`, `Pro Withdrawal`, `Pro Deposit`, and any unmapped type
  resolve by the BTC quantity SIGN** — negative = sell, positive = buy (so a USDC→BTC convert is
  a *buy*, BTC→USDC a *sell*). Income types (`Rewards Income`, etc.) stay income.
- `Total (inclusive of fees and/or spread)` is the basis (buy) / net proceeds (sell) directly —
  no separate fee is applied (it's already in the total). Non-BTC `Asset` rows ignored.
- Note: Coinbase `Send` rows carry no on-chain txid in this export, so a send→self-custody can't
  be auto-connected by shared txid. The reconciliation inbox matches by a shared on-chain address,
  which a CSV row doesn't carry, so it won't auto-suggest this either (amount+date matching was
  removed). Reconnect it manually — edit the send to `transfer_out` and the matching deposit to
  `transfer_in` (set the carried basis) — or, best, load that self-custody wallet as an xpub so
  the deposit gains an on-chain txid and reconciles automatically.

### Strike (Annual Account Statement)
Header: `Transaction ID, Time (UTC), Status, Transaction Type, Amount USD, Fee USD, Amount BTC,
Fee BTC, Description, Exchange Rate, Transaction Hash`. `Purchase`/`Receive` → buy, `Send`/`Sale`/
`Withdrawal` → sell (the app-wide default); amount from `Amount BTC`, fiat from `Amount USD`,
price from `Exchange Rate`, on-chain txid from `Transaction Hash`. **Only BTC rows are kept.** Strike is a dual USD+BTC account: a row with no
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
