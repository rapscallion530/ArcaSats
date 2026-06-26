# Requirements vs. status (gap analysis)

Comparing the Codex-generated requirements (2026-06-17) against what's built. Legend: ✅ done · 🟡 partial · ❌ not yet.

## Tax rules encoded
- ✅ Property treatment; sales/spends/converts are disposals.
- ✅ Buys non-taxable; record date/amount/fees/USD basis/source.
- ✅ Self-transfers non-taxable; distinguish from gifts/sales (kinds + owner-aware).
- ✅ Basis includes fees (buy fee folded into basis).
- 🟡 Lot methods: **FIFO / LIFO / HIFO** built (per-account selectable). Spec-ID / optimization ❌.
- 🟡 Per-wallet (Rev. Proc. 2024-28): we track **per-account** from the start; **transition/safe-harbor wizard ❌**.
- ✅ Gift carryover + dual-basis + zero-basis risk + holding-period tacking (gift statement).
- ❌ Charitable donation handling (only a generic "spend" kind).
- 🟡 1099-DA awareness (documented); **reconciliation ❌**.

## Core product
1. Local-first privacy — ✅ local SQLite, network gated, **Outbound Data Log ✅**, vendored local assets by default, assistant loopback-only.
2. Data sources — ✅ electrs/Tor, configurable public node, xpub watch-only, CSV, read-only API (creds not persisted), manual; single-address import 🟡; full history ✅.
3. Wallet/account model — ✅ accounts as tax locations + **owner**; ❌ tax-treatment class (personal/business/IRA), source-reliability, ownership-confidence; wallet types are import-type not hot/cold/multisig 🟡.
4. **UTXO-aware lot engine — ❌ (biggest divergence).** We track NET movement per wallet per tx + per-account FIFO/LIFO/HIFO, not per-UTXO lots.
5. **Merge/split logic — ❌.** No UTXO-level basis allocation, change-output basis, CoinJoin/batch classification.
6. Transfer classification — 🟡 kinds + manual reclassify + internal-transfer detection; ❌ auto-propose w/ confidence across all categories (fork/airdrop, lost/stolen, mining auto).
7. Gift handling — ✅ sent/received basis, dual-basis, 709 flag, statement; ❌ spouse/divorce/inheritance/trust workflows.
8. **2025 transition wizard — ❌.** (Relevant: user has 2022–2023 coins.)
9. **Exchange reconciliation — ❌.** No exchange-withdrawal ↔ on-chain-deposit matching; no 1099-DA reconcile.
10. Historical pricing — ✅ cache + Coinbase candles + CSV + manual; ❌ immutable snapshots / report versioning / preserve filed values.
11. Reports — ✅ Form 8949 + Schedule D + income + CSV export + gift statement; ❌ open-lots, transfer-audit, missing-data, donation, JSON, CPA ZIP.
12. Security — ✅ no private keys, xpub kept out of git/logs, Tor mode, localhost-only bind; ❌ **DB-at-rest encryption**, encrypted backup/restore.
13. Auditability — ❌ immutable event log + "explain this gain/loss" view.
14. UX — 🟡 dashboard/accounts/tax + node widget; ❌ first-run wizard, timeline, wallet map, UTXO explorer, reconciliation/missing-data inboxes.

## Built beyond the list
Single-user (optional app-wide password lock), in-app node settings (Sparrow-style Tor toggle), node status widget, cross-account basis carry + per-transfer carry on/off, account/wallet/tx edit+delete, auto script-type detection, KYC origin on cost-basis lots + KYC-aware disposal priority, address-based fuzzy-hop detection, price-source registry abstraction, separate node/mempool connections (each tested independently; mempool over Tor), unified "Balance" (holding_sats == account balance; BTC fees reduce both), lossless CSV mapping (raw-row stash + custodian basis/acq-date + CSV→wallet linkage) with an expandable per-tx detail view, 185 tests.

## Assessment of the spec
- **Agree** with ~90%: privacy model, electrs/Tor, gift handling, FIFO default, reconciliation, 1099-DA caution, auditability, immutable pricing.
- **Pushback / reprioritize:**
  - **UTXO-level lot engine is over-weighted for this user.** Rev. Proc. 2024-28's unit is the **wallet/account**, not the UTXO; mainstream tools (Koinly/CoinLedger) track at account/lot level on net movements, like we do. UTXO-level is the gold standard for coin-control/CoinJoin users but a major rewrite — defer unless needed.
  - Source-reliability scores, ownership-confidence, trust/divorce workflows = valuable but gold-plating for a personal tool.
  - Cheap, high-value wins the spec under-weights: **DB-at-rest encryption** and an **Outbound Data Log** (both fit the privacy ethos).

## Roadmap status (built 2026-06-17)
1. ✅ **Exchange ↔ on-chain reconciliation** — txid + amount/date matching, same-owner; carries basis.
2. ✅ **Starting basis / Rev. Proc. 2024-28** — "Opening balance" lot kind (date + USD basis).
3. ✅ **Lot methods** — FIFO / LIFO / HIFO, per account.
4. ✅ **Outbound Data Log** (host+purpose, local) + documented at-rest encryption stance.
   🟡 Live DB-at-rest encryption deferred (needs SQLCipher native lib; OS full-disk encryption recommended meanwhile).
5. ✅ **Audit / "explain this gain/loss"** — open lots + per-disposal lot trace + needs-attention warnings.
6. 🟡 **KYC/UTXO lot engine — Layer A + B(class) shipped; outpoint specific-ID deferred.**
   - ✅ **Layer A — KYC on the lot:** the acquiring account's label is snapshotted onto each
     acquisition (`Transaction.kyc_origin`), carried across self-transfers by rebuilding the
     destination lots from the source fragments (preserving each fragment's original acquisition
     date — so the holding period tacks, IRC §1223 — and its own KYC label). Holdings + realized
     gains break down by KYC class (cost-basis tile, 8949, assistant snapshot).
   - ✅ **Layer B (start) — dispose by KYC status:** `Account.disposal_priority`
     (`non_kyc_first`/`kyc_first`) consumes the preferred KYC class first; within a class the
     account's FIFO/LIFO/HIFO ordering still applies. Specific-ID **by class**; gain math
     unchanged. Default `none` keeps the engine byte-identical (incl. the HIFO fast path).
   - ❌ **Deferred — UTXO-outpoint specific-ID:** picking *literal* coins to dispose needs a
     lot↔UTXO link (only on-chain receives have UTXOs; custodial lots are omnibus) + a
     per-disposal coin-picker UI. See `docs/utxo-tracking.md` Phase 3 (incl. the conservative
     mixed→KYC rule for a consolidation output). A major rewrite for marginal benefit to a
     DCA/low-volume holder; Rev. Proc. 2024-28's unit is the wallet/account, which we track.

Net: all 6 priorities now shipped or substantially advanced; only the UTXO-outpoint level of #6
remains a scoped future epic, not a gap that blocks correct returns for this user.
