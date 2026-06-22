# Design: on-chain (xpub) transaction classification

**Status: BUILT (2026-06-17).** Standalone default shipped; per-wallet `onchain_mode`,
cross-wallet transfer reclassification, direction-keyed dedupe + reclassification migration,
and the mempool "explorer ↗" link are implemented and tested (110 tests).

## Problem

The xpub importer currently labels every net on-chain inflow as `transfer_in` and every net
outflow as `transfer_out` (`app/services/importers/xpub.py:161`), assuming the wallet is
self-custody *fed by an exchange you import separately* (basis comes from the CSV side). For a
**standalone wallet** — coins bought from outside and sold to outside, no exchange CSV — this is
wrong in the dangerous direction: it records taxable **buys/sells as non-taxable transfers** and
reports **$0 cost basis**. (It already nets change within each tx, so that part is fine.)

Correct rule (per the audit discussion): **it's a transfer only if BOTH counterparties are
visible to us** (addresses in wallets we've loaded). An external counterparty means an
acquisition (inflow) or disposal (outflow).

## Chosen model: per-wallet `onchain_mode`, default **standalone**

Add `Wallet.onchain_mode`:
- **`standalone`** (default): external inflow → **BUY**, external outflow → **SELL** (taxable;
  USD supplied by the price feed / user). Use when the wallet's coins are acquired & disposed
  externally and you do NOT import a separate exchange CSV for them.
- **`custodial_fed`**: external inflow/outflow → **transfer** (current behavior). Use when an
  exchange CSV (with the real buy prices) is also imported, so basis carries and isn't
  double-counted.

Regardless of mode, **true internal transfers between your own loaded wallets are detected and
labeled `transfer`** (both sides visible).

## How classification is decided (per on-chain tx, net ≠ 0)

1. **Outflow (sent > received):** inspect the tx's `vout` destination addresses that aren't
   ours-this-wallet. This data is already in the verbose tx (cheap).
   - All external destinations ∈ **KNOWN** (union of addresses across your loaded same-owner
     wallets) → `transfer_out`.
   - Otherwise → **SELL** (standalone) or `transfer_out` (custodial_fed).
2. **Inflow (received > sent):** the source is the input addresses; fetching every external
   input's prev-tx over Tor is expensive, so inflow→transfer detection is done in step 3 by
   txid pairing rather than at scan time.
   - Default → **BUY** (standalone) or `transfer_in` (custodial_fed).
3. **Post-sync cross-wallet reconciliation (extends the existing txid matcher):** a tx that
   appears as an outflow in wallet A and an inflow in wallet B under the **same txid**, both
   same-owner, is a genuine internal transfer → **reclassify both** to `transfer_out`/
   `transfer_in` and carry basis. This already exists for basis-carry; we extend it to *also
   reclassify* default buy/sell rows into transfers when a same-txid counterpart is found.
   - Single-wallet user (this case): no cross-wallet match → receives stay BUY, sends stay
     SELL. Correct.
   - Multi-wallet user: A→B move is recognized and made a transfer. Correct.

KNOWN = union of derived addresses for all of the user's loaded wallets (same owner), gathered
during each wallet's scan and cached for the reconciliation pass.

## Cost basis / proceeds (resolves the original "Fetch USD prices does nothing")

BUY/SELL **are** value-kinds, so **Fetch USD prices** will populate their USD value from the
historical price at the tx time (hourly→daily). Caveats surfaced in the UI:
- These USD figures are **price-feed estimates** (`fiat_source="estimate"`), not the actual
  fiat you paid/received — especially for a SELL, the real proceeds is what the buyer/exchange
  gave you. Edit to set exact values (marks them `manual`).
- A network fee paid on a SELL/spend is itself a small disposal (the existing deferred
  fee-basis item) — still approximate.

## Dedupe / migration (important — you already have 16 old transfer rows)

- `external_id` currently embeds the kind (`{txid}:{kind}`). If a re-sync relabels a row, the
  id changes and you'd get **duplicate** rows. Fix: key the id on **direction** only
  (`{txid}:in` / `{txid}:out`), independent of buy/sell-vs-transfer, so a re-sync matches the
  existing row and reclassifies it **in place**.
- One-time migration for already-imported xpub rows: for each existing `xpub:*` transfer, apply
  the new classification (standalone → buy/sell unless a cross-wallet txid match exists) so your
  current 16 rows become the correct buys/sells without duplicating.

## Edge cases to keep honest

- A self-transfer to a wallet you **haven't loaded yet** looks external → classified SELL until
  you load that wallet and re-sync (then reclassified to transfer). Documented in-app.
- An inflow that's really **income/gift-received**, or an outflow that's a **gift/spend**, needs
  user reclassification (the edit form's kind dropdown already supports this).
- Consolidations within one xpub net to ~0 and are skipped (unchanged).

## Work breakdown (for the build, once approved)

1. `Wallet.onchain_mode` column (default `standalone`) + migration + a select on the wallet
   add/edit form.
2. `scan_xpub`: capture external `vout` destination addresses per tx (for outflow transfer
   detection); expose them on `OnChainTx`.
3. KNOWN-address gathering across same-owner wallets + a helper to test counterparty membership.
4. `import_xpub`: classify per `onchain_mode` + KNOWN set; `external_id` keyed on direction.
5. Extend `reconcile_internal_transfers` to **reclassify** matched cross-wallet buy/sell pairs
   into transfers (in addition to carrying basis).
6. One-time reclassification migration for existing `xpub:*` rows.
7. UI: a "from on-chain — review classification" indicator; note that buy/sell USD is an
   estimate; verify before filing.
8. Tests: standalone buy/sell labeling; cross-wallet transfer auto-detection & reclassify;
   re-sync idempotency after relabel; price backfill now fills the buys/sells.

## Risk / honesty notes

- This makes xpub-only ledgers **tax-meaningful** (real buys/sells with basis) instead of an
  all-transfer black hole — the right direction. But the USD values are **market-price
  estimates**; a standalone wallet genuinely lacks the real fiat amounts, so the user must
  review/edit, and this remains alpha-grade, not filing-ready, without that review.
- Inflow transfer-detection relies on txid pairing across loaded wallets; a transfer in from a
  wallet you never load can't be distinguished from a buy (defaults to buy — safe: not hidden).
