# Code Review & Audit Guide

A map of the codebase for human auditors, plus the current known limitations and open
follow-ups. Pairs with [`../SECURITY.md`](../SECURITY.md) (trust model + reporting). The history
of what past audit passes found and fixed lives in [`../CHANGELOG.md`](../CHANGELOG.md).

License: MIT (see [`../LICENSE`](../LICENSE)). Contributions and audits are welcome.

## Architecture at a glance

```
app/
  main.py            ASGI app + auth/CSRF middleware (the security gate for every request)
  db.py              SQLAlchemy engine, SQLite PRAGMAs, Alembic migration runner
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
    tor_service.py   bundled Tor: verified download + managed daemon        <- egress (desktop)
    outbound.py      Outbound Data Log (host+purpose only)                 <- egress ledger
    auth.py          PBKDF2 + signed session tokens
    accounts.py      account/wallet ops
    assistant.py     read-only "Ask your data" snapshot builder
    node_settings.py Electrum/explorer config + locality heuristics
```

**Money invariants:** Bitcoin is always integer **satoshis**; USD is always **Decimal** with
explicit cent quantization. No floats in arithmetic paths. Auditors should confirm any new
code keeps this.

**Trust boundaries (where data could leave the machine):** `electrum.py`, `pricing.py`,
`llm.py` — all funnel through `outbound.py` for logging. Network is OFF unless
`BTT_ENABLE_NETWORK=1` (price feed) or the user configures a node/LLM. `tor_service.py` (desktop
only) additionally downloads the official Tor binary over HTTPS (sha256-verified) and runs it as a
loopback-only client; it carries no user/portfolio data. See `SECURITY.md`.

**Auth model:** single-user — no user accounts. Open by default; set `BTT_APP_PASSWORD` to gate
the whole instance behind one password (an HMAC-signed unlock cookie). The gate lives in the
`main.py` middleware (`auth.app_lock_enabled` / `verify_unlock`).

## Running the checks

```
pytest -q                          # 221 tests; crypto vectors (mainnet+testnet), raw-tx parse, cost-basis, KYC lots, importers,
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
- **Filing readiness** — a readiness panel now flags missing/estimated USD values + unmatched
  self-transfers on the tax page and 8949, the 8949 is framed as a *draft*, and the CSV carries
  a methodology/provenance footer (lot method + price source). Still not modeled: explicit
  gift/donation/mining/inheritance/lost-coin classifications, and per-row price provenance.

### Schema / migrations
- **Migrations: Alembic** ✅ — the schema is versioned under `alembic/` and applied at startup
  (`db.init_db` → `alembic upgrade head`; a pre-Alembic DB is stamped at the baseline first, then
  migration `0002` drops the orphaned `users`/`owner_user_id`/`allow_remote`). The old hand-rolled
  `ALTER`/`CREATE INDEX` runner is gone.
- **`PRAGMA foreign_keys=ON` deferred** — now a straightforward Alembic migration away (add the
  ON DELETE rules + table rebuild, then flip the pragma). ORM cascades handle child cleanup
  meanwhile.
- **Existing-DB dedup** — older DBs still carry the old global `(source, external_id)` uniqueness;
  new DBs (the Alembic baseline) use `(account_id, source, external_id)`. A small Alembic
  table-rebuild migration can now drop the old constraint (it over-dedups across accounts meanwhile).

### Privacy / egress
- **Centralize the locality check** — `llm.is_local`, `electrum._is_lan_host`,
  `node_settings.explorer_is_private`, and pricing's gating each implement their own policy; a
  single audited `assert_local_or_allowed(url)` would ensure no future connector ships without
  the gate.
- **DNS rebinding** between the locality check and connect is mitigated for the assistant
  (loopback-only + redirect blocking) but not fully eliminated for arbitrary hostnames.
- **Abstract the price source** — the Coinbase/Bitstamp URLs/schema are hardcoded; an interface
  plus cached "no data" hours would harden and speed it up.

### On-chain sync (Electrum)
- **Verbose-tx not required (resolved).** The xpub scanner (`importers/xpub.py`) prefers verbose
  `blockchain.transaction.get`, but now **falls back to fetching raw tx hex and parsing it locally**
  (`services/txparse.py` + `script.scriptpubkey_to_address`) for servers that reject verbose (e.g.
  **blockstream's public electrs** answers *"verbose transactions are currently unsupported"*). Raw
  txs carry no block time, so dates come from the block header at the tx's height
  (`ElectrumClient.block_header`). Verified live against blockstream on both networks (mainnet
  `bc1…`, testnet `tb1…`) — real outputs/values/dates decode correctly.

### Auth / ops
- **Optional password lock** has no rate-limiting/lockout (low priority — one local password).
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
