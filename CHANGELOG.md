# Changelog

Hardening and audit history. The current architecture map, known limitations, and open
follow-ups live in [`docs/code-review.md`](docs/code-review.md); this file records what changed.

Severity tags: **P0** correctness/security/privacy · **P1** performance · **P2** best practice.

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
