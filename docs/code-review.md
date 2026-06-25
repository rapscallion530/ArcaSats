# Code Review & Audit Guide

A map of the codebase for human auditors, plus the current known limitations and open
follow-ups. Pairs with [`../SECURITY.md`](../SECURITY.md) (trust model + reporting). The history
of what past audit passes found and fixed lives in [`../CHANGELOG.md`](../CHANGELOG.md).

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
    pricing.py       BTC/USD price cache + Coinbase/Bitstamp fetch          <- egress
    llm.py           local-LLM client + loopback/LAN privacy gate          <- egress
    outbound.py      Outbound Data Log (host+purpose only)                 <- egress ledger
    auth.py          PBKDF2 + signed session tokens
    accounts.py      account/wallet ops + owner-scope authorization helpers
    assistant.py     read-only "Ask your data" snapshot builder
    node_settings.py Electrum/explorer config + locality heuristics
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
pytest -q                          # 143 tests; crypto vectors, cost-basis, importers, auth,
                                   #   IDOR, CSRF, pricing, tz
python scripts/release_check.py    # release-hygiene gate (no secrets tracked, doc test-count
                                   #   matches collected, vendored assets present)
```

## Known limitations & open follow-ups

These are deliberately deferred and documented, not hidden. Roughly by area:

### Tax / accounting
- **On-chain fee in basis** — an xpub send folds the payment + miner fee into one transfer
  amount; the fee is itself a small disposal and needs separate treatment.
- **Wallet pooling vs. tax segregation** — reports are per-account; several wallets per account
  aren't separately lot-tracked. Guidance is one account per exchange/custodial location
  (not enforced, since that would break legitimate multi-xpub self-custody). A true
  per-wallet/UTXO lot engine is the alternative.
- **UTXO-level / multi-asset lots** — adding an `asset` column (default `BTC`) now would avoid a
  painful migration later. (HIFO lot selection is now heap-backed — O(n log n) — so it already
  scales to large ledgers; see `_dispose`/`hifo_heap` in `costbasis.py` and the perf regression
  test.)
- **Filing readiness** — no missing-data checklist yet, nor explicit
  gift/donation/mining/inheritance/lost-coin classifications.

### Schema / migrations
- **Additive-only migrations** — `db.py` does `ALTER TABLE`/`CREATE INDEX` (plus one guarded
  `DROP COLUMN`); it can't do renames or ordered backfills. Adopt **Alembic** before the schema
  grows further.
- **`PRAGMA foreign_keys=ON` deferred** — first add `ON DELETE SET NULL` to
  `accounts.owner_user_id` (a table-rebuild migration) so the lockout-reset (deleting `users`
  rows) still works. ORM cascades handle child cleanup meanwhile.
- **Existing-DB dedup** — older DBs still carry the old global `(source, external_id)`
  uniqueness; new DBs use `(account_id, source, external_id)`. A table-rebuild migration would
  drop the old constraint (it over-dedups across accounts meanwhile).

### Privacy / egress
- **Centralize the locality check** — `llm.is_local`, `electrum._is_lan_host`,
  `node_settings.explorer_is_private`, and pricing's gating each implement their own policy; a
  single audited `assert_local_or_allowed(url)` would ensure no future connector ships without
  the gate.
- **Per-owner reconcile scope** — `reconcile_internal_transfers`/`internal_txids` operate
  globally; harmless in single-user/open mode, but in multi-user this crosses owner boundaries.
- **DNS rebinding** between the locality check and connect is mitigated for the assistant
  (loopback-only + redirect blocking) but not fully eliminated for arbitrary hostnames.
- **Abstract the price source** — the Coinbase/Bitstamp URLs/schema are hardcoded; an interface
  plus cached "no data" hours would harden and speed it up.

### Auth / ops
- **Login rate-limiting / lockout**, and **rehash-on-login** when PBKDF2 params change.
- **Secure-cookie flag** when served over TLS; **WAL-aware backup** (SQLite backup API, not a
  bare file copy); **background sync** with progress/cancel.

### Packaging
- Run Docker as **non-root** and **checksum** the Tailwind binary download.

## Unsupported derivation (by design, for now)
- **Taproot (p2tr)** derivation, and arbitrary non-`multi` descriptor fragments. Native SegWit,
  Nested SegWit, Legacy, and `(sorted)multi` multisig descriptors are supported.

## What's already solid (don't "fix")

- Watch-only by design — no private-key handling anywhere; worst case is a privacy, not theft,
  loss. PBKDF2-HMAC-SHA256 (200k) with constant-time compare. Network off by default.
- Clean money handling (int sats + Decimal), correct IRS long-term boundary (>365 days),
  the same-owner-internal vs different-owner-gift distinction, and the refusal to fabricate
  transfer-in basis from a display price.
- Jinja autoescaping on; the one `Markup`-returning filter escapes its dynamic value.

## Hardening history

What prior audit passes found and fixed is recorded in [`../CHANGELOG.md`](../CHANGELOG.md).
