# Code Review & Audit Guide

A map of the codebase for human auditors, plus the findings from the pre-release audit
(2026-06-17) — what was fixed and what remains as recommended follow-ups. Pairs with
[`../SECURITY.md`](../SECURITY.md) (trust model + reporting).

License: MIT (see [`../LICENSE`](../LICENSE)). Contributions and audits are welcome.

## Architecture at a glance

```
app/
  main.py            ASGI app + auth/CSRF middleware (the security gate for every request)
  db.py              SQLAlchemy engine, SQLite PRAGMAs, lightweight additive migrations
  models.py          ORM models (money: BTC=int sats, USD=Decimal) + indexes
  config.py          env-driven settings (network OFF by default)
  templating.py      Jinja env + money filters (autoescape on)
  routers/           HTTP handlers (thin) — accounts, tax, settings, assistant, auth, dashboard
  services/          domain logic (no HTTP):
    costbasis.py     FIFO/LIFO/HIFO lot engine, transfer/gift basis carry  <- tax correctness
    bip32.py         pure-Python secp256k1 + BIP32 watch-only derivation   <- crypto
    script.py        address -> scriptPubKey -> Electrum scripthash        <- crypto
    electrum.py      Electrum JSON-RPC client (Tor SOCKS5)                  <- egress
    pricing.py       BTC/USD price cache + Coinbase fetch                   <- egress
    llm.py           local-LLM client + is_local() privacy gate            <- egress
    outbound.py      Outbound Data Log (host+purpose only)                 <- egress ledger
    auth.py          PBKDF2 + signed session tokens
    accounts.py      account/wallet ops + owner-scope authorization helpers
    assistant.py     read-only "Ask your data" snapshot builder
```

**Money invariants:** Bitcoin is always integer **satoshis**; USD is always **Decimal** with
explicit cent quantization. No floats in arithmetic paths. Auditors should confirm any new
code keeps this.

**Trust boundaries (where data could leave the machine):** `electrum.py`, `pricing.py`,
`llm.py` — all funnel through `outbound.py` for logging. Network is OFF unless
`BTT_ENABLE_NETWORK=1` (price feed) or the user configures a node/LLM. See `SECURITY.md`.

**Auth model:** "open mode" (no users → no login, single-user local) vs "secured mode" (an
admin exists → login required, accounts scoped to their owner). The middleware in `main.py`
and the `accessible_*` helpers in `services/accounts.py` are the enforcement points.

## Running the checks

```
pytest -q            # 108 tests; crypto vectors, cost-basis, importers, auth, IDOR, CSRF, pricing, tz
```

## Audit findings — fixed in this pass

Severity: P0 correctness/security/privacy · P1 performance · P2 best practice · P3 minor.

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

## Second audit (independent) — addressed 2026-06-17

A second reviewer found defects the first pass missed; these are now fixed:

- **[P0] Transfer-in FMV became basis.** `_acq_basis` returned `fiat_value` before the
  carryover check, so an exchange "receive" row's receipt-time USD silently set basis.
  Transfer-ins now NEVER use `fiat_value` — basis is carryover (or 0, with a warning).
  Test: `test_transfer_in_fiat_value_is_not_basis`.
- **[P0] Reconciliation crossed users.** Blank owner labels on different users' accounts
  compared equal. Owner identity is now `(owner_user_id, owner-label)`; matching never crosses
  `owner_user_id`. Test: `test_reconcile_does_not_cross_user_boundary`.
- **[P0] Assistant could export to remote / via redirect.** `allow_remote` is removed; the
  assistant is hard local-only (re-checked at call time) and HTTP redirects are blocked
  (`_NoRedirect` opener) to stop a post-check redirect from carrying data off-box.
- **[P0] CDN assets by default.** `BTT_ASSETS` now defaults to **local**; `tailwind.css` is
  built and `htmx.min.js` vendored, so an ordinary launch makes no external requests.
- **[P0] Price requests leaked exact tx timestamps.** Backfill now warms prices a **whole
  month at a time** (`warm_hourly_months`) regardless of which days have txs, and `price_at`
  no longer fetches a single tx's exact hour — only the month of activity is revealed.
- **[P0] Importer silently corrupted/omitted rows.** Bad dates (was 1970) and zero/invalid
  amounts are now **rejected with reasons** (not coerced); parser-dropped rows (unknown type /
  non-BTC) are surfaced; the UI shows rejects distinctly from duplicates. Test:
  `test_bad_rows_are_rejected_not_silently_coerced`.
- **[P0] Heuristic transfer matching auto-mutated basis.** Only exact-txid matches auto-apply;
  amount+date heuristics require `include_heuristic=True` (a future review/approval inbox).
- **[High] settings model-list XSS** (escaped), **gift exclusion** now per-year, **long-term**
  uses the leap-year-correct anniversary test, **open-mode + non-loopback bind** warns at start.
- **Per-wallet tax claim** corrected to per-account in README/UI (engine is per-account; the
  per-wallet view is informational).

## Third audit (independent) — addressed 2026-06-17

- **[P0] Timezone offsets dropped, not converted.** Importers/connectors did
  `.replace(tzinfo=None)`, keeping local clock; an offset like `+05:00` could shift the tax
  date/year/price hour. New `to_naive_utc()` converts aware timestamps to UTC first (csv +
  Coinbase + Strike). Test: `test_offset_timestamp_converts_to_utc`.
- **[P0] Long-term anniversary time-of-day error.** `_is_long_term` now compares calendar
  DATES (a sale any time on the one-year anniversary is short-term; must be a later date).
- **[P0] Open-mode network takeover only warned.** Now an enforcement boundary: bound to a
  non-loopback interface in open mode, the app **refuses to start** unless `BTT_SETUP_TOKEN`
  (which then gates `/setup`) or `BTT_ALLOW_OPEN_EXPOSURE=1` (for a platform with its own auth
  gateway) is set. Docker sets `BTT_BIND_HOST=0.0.0.0` so the guard is reality-aware; the
  Umbrel compose sets the escape (behind Umbrel's authenticated app_proxy).
- **[P0 privacy] Assistant tightened to loopback-only.** It now talks ONLY to a model on THIS
  machine by default (`is_loopback`); a LAN model requires `BTT_ASSISTANT_ALLOW_LAN=1`. Public
  endpoints refused, redirects blocked. Test: `test_assistant_is_loopback_only_by_default`.
- **[High] Account-scoped dedup** — the model's uniqueness is now
  `(account_id, source, external_id)` (was global), so the same export into two accounts isn't
  cross-dropped. Test: `test_dedup_is_account_scoped`. (Existing DBs keep the old global
  constraint until a table rebuild — see below.)
- **[High] Edit no longer zeroes `fee_sats`** (preserved when the form omits it); **standalone
  FEE** now reduces the account balance; **gift exclusion** warns for years outside its table;
  **daily-price fallback** is now recorded in the Outbound Data Log.
- **Docs reconciled:** README CDN/no-persistence sections corrected; SECURITY.md describes the
  loopback-only assistant; status doc no longer says "FIFO only"/"no outbound log"; packaging
  `yourname` placeholders replaced.

### Still open from this audit (deferred, documented)
- **On-chain fee distorts carried basis** — an xpub send folds payment + miner fee into one
  transfer amount; the fee's basis needs separate treatment (a fee is itself a small disposal).
- **Wallet pooling vs. tax segregation** — the UI allows several wallets per account; reports
  are per-account. Guidance is to use one account per exchange/custodial location; not enforced
  (enforcing would break legitimate multi-xpub self-custody) — a true per-wallet lot engine is
  the alternative.
- **Existing-DB dedup** still carries the old global `(source, external_id)` constraint until a
  table-rebuild migration (new DBs are correct).
- **DNS rebinding** between `is_local()` and connect is mitigated for the assistant by
  loopback-only + redirect blocking, but not fully eliminated for arbitrary hostnames.

## Recommended follow-ups (not done this pass)

- **Enable `PRAGMA foreign_keys=ON`** — but first add `ON DELETE SET NULL` to
  `accounts.owner_user_id` (needs a table-rebuild migration) so the lockout-reset (deleting
  `users` rows) still works. Currently deferred to avoid regressing that flow; child cleanup is
  handled by ORM cascades meanwhile.
- **Adopt Alembic** before the schema grows further. The additive `ALTER TABLE`/`CREATE INDEX`
  approach in `db.py` can't do renames, drops, or ordered backfills.
- **Centralize the egress locality check.** `llm.is_local`, `electrum._is_lan_host`, and
  `pricing`'s network gating each implement their own policy; a single audited
  `assert_local_or_allowed(url)` would ensure no future connector ships without the gate.
- **Scope `reconcile_internal_transfers`/`internal_txids` per owner.** They currently operate
  globally; harmless in single-user/open mode, but in multi-user this crosses owner boundaries.
- **Abstract the price source** behind an interface (Coinbase URL/schema is hardcoded) and
  consider batched range fetches; cache "no data" hours to avoid re-fetching.
- **Multi-asset / UTXO-level lots.** Add an `asset` column (default `BTC`) now to avoid a
  painful migration later; the HIFO lot selector is O(lots²) and would need a heap at UTXO scale.
- **Login rate-limiting / lockout**, and **rehash-on-login** when PBKDF2 params change.
- **Per-file vs LICENSE.** All files carry an SPDX header; the `LICENSE` file is authoritative.
- **Dedup scope on existing DBs.** The model's uniqueness is now `(account_id, source,
  external_id)` for new DBs; existing DBs still carry the old global `(source, external_id)`
  constraint until a table-rebuild migration drops it (over-dedups across accounts meanwhile).
- **Exchange-API connectors removed** (Coinbase/Strike) in favor of CSV import — direct API
  sync was a half-working stub and conflicted with local-only; CSV is the supported path.
- **Mempool transactions** get the current time and aren't refreshed after confirmation; **on-chain
  sends** fold the recipient amount + miner fee into one transfer amount (distorts carried basis).
- ✅ **xpub script detection** now probes the first N addresses on BOTH chains (was index-0 only),
  with a manual **address-type override** on the wallet form. ✅ **Multisig** via output-descriptor
  import (`wsh/sh/sh(wsh)` of `(sorted)multi` → P2WSH/P2SH/P2SH-P2WSH, `app/services/descriptor.py`).
  Still unsupported: **Taproot (p2tr)** derivation, and arbitrary non-`multi` descriptor fragments.
- **Background sync** with progress/cancel; **secure-cookie** flag when served over TLS; **WAL-aware
  backup** (SQLite backup API / snapshot, not a bare file copy).
- **UX for safety:** import preview, a reconciliation inbox for heuristic matches, a missing-data /
  filing-readiness checklist, and explicit gift/donation/mining/inheritance/lost-coin classifications.
- **Open-source hygiene:** pin dependencies, run Docker as non-root, checksum the Tailwind binary,
  replace `yourname` packaging placeholders, and add CI (lint/type/test/security).

## What's already solid (don't "fix")

- Watch-only by design — no private-key handling anywhere; worst case is a privacy, not theft,
  loss. PBKDF2-HMAC-SHA256 (200k) with constant-time compare. Network off by default.
- Clean money handling (int sats + Decimal), correct IRS long-term boundary (>365 days),
  the same-owner-internal vs different-owner-gift distinction, and the refusal to fabricate
  transfer-in basis from a display price.
- Jinja autoescaping on; the one `Markup`-returning filter escapes its dynamic value.
