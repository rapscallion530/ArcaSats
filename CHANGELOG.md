# Changelog

Hardening and audit history. The current architecture map, known limitations, and open
follow-ups live in [`docs/code-review.md`](docs/code-review.md); this file records what changed.

Severity tags: **P0** correctness/security/privacy · **P1** performance · **P2** best practice.

## Unreleased — balance vs holdings consistency

- **[P0] Cost-basis "Holdings" overstated by BTC network fees.** The account balance
  (`accounts._balance_expr`, shown on the grid + tx-table) subtracts every `fee_sats`, but the
  cost-basis engine (`CostBasisResult.holding_sats`) ignored `fee_sats` and standalone `FEE` txs —
  so a Strike Send carrying an on-chain "Fee BTC" made the tile read higher than the balance (the
  reported 16 vs 11 sats). The balance is correct (the fee BTC left the wallet); `compute` now
  consumes `fee_sats` (and a standalone `FEE`'s amount) from the lots as a **non-realizing**
  reduction (sats + basis), even for internal-within transfers, so `holding_sats` equals the
  account balance. A new invariant test asserts `holding_sats == balance_sats` across
  buys/sells/fee/transfer/Strike-Send cases so they can't drift. (Realizing the fee's own
  gain/loss — miner-fee-as-disposal — stays deferred; we only drop its quantity + basis.)
- **UI:** the tile's "Holdings" → **"Balance"** (and "Holdings by KYC origin" → "Balance by KYC
  origin"), matching the grid + tx-table; one consistent number. (180 tests.)

## Unreleased — separate node & mempool connections

Settings lumped the Electrum **node** and the **mempool** into one form with one "Test
connection" button that only ever tested the node; the mempool was never exercised, and the app's
mempool fetch (the `mempool` price source) used plain HTTP with no Tor — so a `.onion` mempool
couldn't be reached by the app at all.

- Split Settings into independent **Node connection** and **Mempool connection** sections, each
  with its own Save and Test. `node_settings.save_config` → `save_node` + `save_mempool` so one
  form never clobbers the other's fields.
- New **Test mempool** probes the historical-price API (the app's actual mempool use) and reports
  reachable-with-price / reachable-but-no-price (enable indexing) / unreachable.
- **Mempool over Tor:** new `mempool_use_tor` (migration `0005`) routes the price-API fetch + the
  test through the Tor SOCKS proxy for a `.onion` host (or opt-in), reusing the Electrum client's
  `_socks5_connect` via a new dependency-free `http_fetch.get_json` (clearnet via urllib; Tor via
  raw socket + SOCKS5, TLS only for https, chunked-aware). Explorer links remain browser-side.
- No change to Electrum sync, FMV math, or the weekly-window price privacy model. +9 tests (177).

## Unreleased — efficiency + cleanup pass

- **[P1] Batched import commits.** `transactions.add_transaction` gained `commit=False`;
  `csv_import.persist_records` now preloads the source's existing `external_id`s, de-dupes in
  Python, and writes new rows in a **single** transaction (was a commit + IntegrityError
  round-trip per row). `xpub.import_xpub` likewise loads existing rows once and commits once.
- **[P1] Fewer per-request queries.** `account_detail` computes `internal_txids` and the node
  config once and reuses them (`compute_account_breakdown` accepts a precomputed `internal` set);
  the config was fetched twice and `internal_txids` ran twice per load.
- **[P1] FIFO consumption O(n²) → O(n).** Default FIFO advanced via `del lots[0]` (an O(n) list
  shift per consumed lot); it now walks a forward cursor and leaves spent lots in place (filtered
  from `open_lots`). Results byte-identical (guarded by a full-consume perf test).
- **[P2] Shared HTTP helper.** The `Request → urlopen → json.loads` boilerplate duplicated across
  the five price fetchers is now `pricing._get_json` (llm.py keeps its own redirect/locality-gated
  client).
- **[P2] CSV row normalization once.** `import_csv` normalizes row keys/values a single time;
  parsers no longer each call `_norm_keys` per row.

No behavior change (FMV, cost basis, import dedup all identical). (168 tests.)

## Unreleased — price-source abstraction

Per-source pricing behavior was scattered across five parallel module dicts/tuples
(`THIRD_PARTY_SOURCES`, `PRICE_SOURCES`, `_MAX_HOURS_PER_REQUEST`, `_SOURCE_HOST`,
`_CANDLE_FETCHERS`) + `if source == …` branches, and the valid-source list was duplicated in three
places (pricing, `node_settings`, `settings.html`). Collapsed into one **`PriceSource` registry**
(`pricing.SOURCES`) holding each source's label/host/fetchers/warm-strategy; `PRICE_SOURCES`,
config validation (`node_settings.save_config`), and the Settings dropdown all derive from it, so
a source is now a single registry entry. `get_price` / `price_at` / `backfill_prices` dispatch
through it with no source-name branches.
- **[P1] Batched candle-cache warming.** Third-party week-warming collected candles and wrote them
  with a SELECT + commit **per candle**; it now does one range existence check + a **single
  commit** per warm (mempool warming likewise commits once). Behavior/cache contents unchanged.

No change to FMV math, the 15m-candle-then-daily fallback, the weekly-window privacy model, or
locked-value rules. (167 tests.)

## Unreleased — address-based fuzzy-hop detection (amount+date matching removed)

The reconciliation inbox matched candidate self-transfers on amount + date (±0.002 BTC, ≤7 days)
— fragile when a hop changes the sats (fees/partials/batching) or spans a long time, and prone to
mispairing. That method is **removed**. Detection is now **address-only**: the xpub scanner
records the foreign address one hop from each of our txs (a spend's destination — free, from the
vout we already fetch; an inflow's funder — via a prevtx fetch, since a vin carries only
`txid:vout`), and `suggest_transfers` matches a known→unknown→known hop by that shared
intermediary address, robust to amount/time drift. A hop with no shared address simply isn't
suggested. The auto reconciler likewise only carries proven **shared-txid** transfers (the
`include_heuristic` amount+date auto-carry is gone). New `hop_addresses` table + migration `0004`;
endpoints populate on the next Sync. Inward-only (one hop from your own coins; addresses are
local, never egressed); a confirmed fuzzy hop still carries basis coarsely (no fragment rebuild).
New inbox UI shows the shared address. (164 tests.)

## Unreleased — KYC/UTXO lot engine (audit #8)

KYC provenance through the cost-basis engine — Layer A in full, plus the start of Layer B.
See [`docs/utxo-tracking.md`](docs/utxo-tracking.md) and [`project notes`]. Local-only; no
egress behavior change. (Migration `0003_kyc_origin`; 156 tests.)

### Features
- **Layer A — KYC on the lot.** `Transaction.kyc_origin` snapshots the acquiring account's
  `label_kind` onto each buy/income/opening at import (mirrors `Utxo.label_kind`); relabeling an
  account re-snapshots its existing direct acquisitions. `Lot`/`Disposal` carry the label and
  `CostBasisResult.holding_by_kyc` / `realized_by_kyc` report holdings + gains by class — surfaced
  on the cost-basis tile, the 8949 (`taxforms.totals_by_kyc`), and the assistant snapshot.
- **[P0] Fragment-rebuild basis carry (shared-txid self-transfers only).** A reconciled
  **shared-txid** self-transfer now stores the consumed **source-lot fragments**
  (`Transaction.carried_lots`, JSON) so `compute()` rebuilds the destination lots preserving each
  fragment's **original acquisition date** and KYC label. This also fixes a latent bug where a
  self-transfer collapsed to one lot dated at the transfer, **resetting the holding-period clock**
  (a >1yr-held coin could be misreported short-term after moving wallets). Holding period now
  tacks across transfers (IRC §1223). Fuzzy links (no shared txid) are **not** fragment-carried —
  a hop through an intermediary we can't prove is yours is a final break of ownership (default no
  carry; confirming the address-matched pair in the inbox still carries the basis coarsely as a
  single lot, without tacking the holding period).
- **Layer B (start) — dispose by KYC status.** `Account.disposal_priority`
  (`non_kyc_first`/`kyc_first`) consumes the preferred KYC class first; within a class the
  account's FIFO/LIFO/HIFO ordering still applies. Specific-ID **by class**; gain math unchanged.
  Default `none` keeps selection byte-identical, including the HIFO max-heap fast path.
- Deferred: UTXO-outpoint specific-ID (pick literal coins) — needs a lot↔UTXO link + a picker
  UI; designed in `docs/utxo-tracking.md` Phase 3 (incl. the conservative mixed→KYC rule).

## 0.1.0 — 2026-06 (pre-release hardening)

Three independent audit passes before the public alpha. Findings fixed, grouped by area.

### Security
- **[P0] IDOR across account/wallet/tx/tax routes.** In secured mode, handlers trusted a
  guessable integer id with no owner check. Centralized `accessible_account/_wallet/_tx`
  helpers (`services/accounts.py`) now gate **every** account-keyed route in
  `routers/accounts.py` and `routers/tax.py`. Regression test: `tests/test_security.py`.
- **[P0] `is_local()` privacy gate bypass.** The LLM gate trusted `.local`/`.internal`
  hostnames by name. It now **resolves** the host and requires *all* resolved addresses to be
  loopback/private/link-local (fails closed on unresolvable names), and handles IPv4-mapped
  IPv6. (`services/llm.py`.)
- **[P0] Electrum TLS verification disabled for clearnet.** Certificates are now verified for
  public hosts (a MITM there would learn your scripthashes/addresses); verification is dropped
  only for LAN/loopback and `.local`/`.onion`, where self-signed electrs is normal.
  (`services/electrum.py`.)
- **[P0] Session tokens never expired or could be revoked.** Tokens now embed an issued-at
  (30-day server-side expiry) and a `token_version` (bump to invalidate all of a user's
  sessions — `auth.bump_token_version`). (`services/auth.py`, `models.py`, `main.py`.)
- **[P0] No CSRF defense beyond SameSite.** The middleware now rejects state-changing requests
  whose `Origin`/`Referer` host doesn't match (missing header is allowed for non-browser
  clients/tests). (`main.py`.)
- **[P2] CSV upload unbounded.** `import/csv` now reads with a 10 MB ceiling.
- **[P1] `secret.key` permissions.** Written `0o600` (best effort; no-op on Windows ACLs).

### Correctness (tax engine)
- **[P0] Per-wallet basis double-count.** `compute_wallet` detected intra-account transfers
  only within the wallet subset, so a move between two wallets of one account double-counted
  basis. Internal-transfer detection is now computed at the **account** level and shared with
  every per-wallet view via `compute_account_breakdown`. Test:
  `test_per_wallet_basis_consistent_with_account`.
- **[P0] Over-disposal term.** A disposal exceeding tracked lots is recorded zero-basis,
  short-term (conservative), now with a clear warning telling the user to add an Opening-balance
  lot to correct gain & holding period.

### Performance
- **[P1] N+1 query fan-out** in `all_summaries`/`summarize`/`balance_sats` → grouped
  conditional-aggregate queries (`services/accounts.py`).
- **[P1] Triple full-ledger recompute** on the account detail page → single-pass
  `compute_account_breakdown` (one tx load + one `internal_txids` per request).
- **[P1] Reconciler** recomputed each source account per matched pair and committed per row →
  memoized per account, single commit.
- **[P1] Missing indexes.** Added `(account_id, timestamp)`, `txid`, `kind`, and
  `wallets.account_id`, with a `CREATE INDEX IF NOT EXISTS` migration for existing DBs.
- **[P1] Auth middleware hit the DB on every request** (incl. `/static`) → static/health/
  favicon now skip the session entirely.
- **[P1] Pure-Python scalar multiply** did a modular inverse per point-add (~256/mul) →
  rewritten in **Jacobian coordinates** (one inverse per multiply); validated against the
  existing BIP32/BIP84 vectors.
- **[P1] SOCKS5 short-read framing bug** (fixed-size `recv` assumed whole packets) → `_recv_exact`.

### Robustness / best practice
- WAL + `synchronous=NORMAL` PRAGMAs for concurrent HTMX requests (`db.py`).
- Price fetchers catch *specific* errors and validate response shape (a Coinbase schema change
  degrades predictably instead of silently); hourly fetch requires an exact-hour match or falls
  back to the daily close. (`services/pricing.py`.)
- Electrum response size cap (anti-OOM); on-curve check in `bip32._decompress`; BIP173 witness
  length/version validation in `script._segwit_decode`; tighter `/v1` endpoint detection and a
  `FiatSource` constants class to replace magic strings.

### Second audit (independent)
- **[P0] Transfer-in FMV became basis.** `_acq_basis` returned `fiat_value` before the
  carryover check, so an exchange "receive" row's receipt-time USD silently set basis.
  Transfer-ins now NEVER use `fiat_value` — basis is carryover (or 0, with a warning).
  Test: `test_transfer_in_fiat_value_is_not_basis`.
- **[P0] Reconciliation crossed users.** Blank owner labels on different users' accounts
  compared equal. Owner identity is now `(owner_user_id, owner-label)`; matching never crosses
  `owner_user_id`. Test: `test_reconcile_does_not_cross_user_boundary`.
- **[P0] Assistant could export to remote / via redirect.** The assistant is hard local-only —
  the chat gate (`assistant_endpoint_allowed()`) permits only loopback by default (LAN via
  `BTT_ASSISTANT_ALLOW_LAN=1`), re-checked at call time, and HTTP redirects are blocked
  (`_NoRedirect` opener). *(A legacy per-connection `allow_remote` field once shadowed this gate;
  it was unused by `chat()` and has since been removed — the column is dropped from existing DBs
  by a guarded migration in `db.py`.)*
- **[P0] CDN assets by default.** `BTT_ASSETS` now defaults to **local**; `tailwind.css` is
  built and `htmx.min.js` vendored, so an ordinary launch makes no external requests.
- **[P0] Price requests leaked exact tx timestamps.** Backfill now warms prices a **whole
  week at a time** (`warm_candle_weeks`, fixed Mon–Sun) regardless of which days have txs, and
  `price_at` no longer fetches a single tx's exact hour — only the week of activity is revealed.
- **[P0] Importer silently corrupted/omitted rows.** Bad dates (was 1970) and zero/invalid
  amounts are now **rejected with reasons** (not coerced); parser-dropped rows are surfaced; the
  UI shows rejects distinctly from duplicates. Test: `test_bad_rows_are_rejected_not_silently_coerced`.
- **[P0] Heuristic transfer matching auto-mutated basis.** Only exact-txid matches auto-apply;
  amount+date heuristics require `include_heuristic=True` (the review/approval inbox).
- **[High]** settings model-list XSS (escaped); gift exclusion now per-year; long-term uses the
  leap-year-correct anniversary test; open-mode + non-loopback bind warns at start.
- **Per-wallet tax claim** corrected to per-account in README/UI (engine is per-account; the
  per-wallet view is informational).

### Third audit (independent)
- **[P0] Timezone offsets dropped, not converted.** Importers/connectors did
  `.replace(tzinfo=None)`, keeping local clock; an offset like `+05:00` could shift the tax
  date/year/price hour. New `to_naive_utc()` converts aware timestamps to UTC first (csv +
  Coinbase + Strike). Test: `test_offset_timestamp_converts_to_utc`.
- **[P0] Long-term anniversary time-of-day error.** `_is_long_term` now compares calendar
  DATES (a sale any time on the one-year anniversary is short-term; must be a later date).
- **[P0] Open-mode network takeover only warned.** Now an enforcement boundary: bound to a
  non-loopback interface in open mode, the app **refuses to start** unless `BTT_SETUP_TOKEN`
  (which then gates `/setup`) or `BTT_ALLOW_OPEN_EXPOSURE=1` is set. Docker sets
  `BTT_BIND_HOST=0.0.0.0` so the guard is reality-aware; the Umbrel compose sets the escape
  (behind Umbrel's authenticated app_proxy).
- **[P0 privacy] Assistant tightened to loopback-only.** It now talks ONLY to a model on THIS
  machine by default (`is_loopback`); a LAN model requires `BTT_ASSISTANT_ALLOW_LAN=1`. Public
  endpoints refused, redirects blocked. Test: `test_assistant_is_loopback_only_by_default`.
- **[High] Account-scoped dedup** — the model's uniqueness is now
  `(account_id, source, external_id)` (was global), so the same export into two accounts isn't
  cross-dropped. Test: `test_dedup_is_account_scoped`. (Existing DBs keep the old global
  constraint until a table rebuild — see `docs/code-review.md`.)
- **[High]** Edit no longer zeroes `fee_sats`; standalone FEE now reduces the account balance;
  gift exclusion warns for years outside its table; daily-price fallback is recorded in the
  Outbound Data Log.
- **xpub script detection** now probes the first N addresses on BOTH chains (was index-0 only),
  with a manual **address-type override** on the wallet form. **Multisig** via output-descriptor
  import (`wsh/sh/sh(wsh)` of `(sorted)multi` → P2WSH/P2SH/P2SH-P2WSH, `services/descriptor.py`).
- **Docs reconciled:** README CDN/no-persistence sections corrected; SECURITY.md describes the
  loopback-only assistant; status doc corrected; packaging `yourname` placeholders replaced.

### Removed
- **Direct exchange-API connectors (Coinbase/Strike).** The API sync was a half-working stub
  that conflicted with local-only; CSV import is the supported path.

## Post-0.1.0 (unreleased)
- Real-format CSV importers for Swan, Strike (incl. bill-pay), and Coinbase Transaction history.
- UTXO inventory + privacy lints (KYC/non-KYC merge, address reuse, toxic change).
- Reconciliation inbox for no-shared-txid self-transfers (suggest / confirm / reject).
- App-wide rule: a BTC movement is a taxable buy/sell by default, a transfer only when connected
  to another of your own wallets.
- Removed the dead `LLMConnection.allow_remote` field (+ guarded `DROP COLUMN` migration).
- Block-explorer privacy warning when the configured explorer host isn't local/`.onion`;
  external-link affordance on the Support links.
- `scripts/release_check.py` release-hygiene gate (wired into CI).
