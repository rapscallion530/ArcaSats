# UTXO-level tracking & privacy analysis

ArcaSats tracks individual on-chain outputs (UTXOs) for xpub/descriptor wallets, on top of the
net-per-transaction ledger used for tax accounting. This is "chain analysis turned inward": it
reasons **only about coins you own and have loaded**, to surface provenance and privacy
exposure — never third parties. It does not touch the cost-basis engine.

Custodial CSV sources (Coinbase/Strike/Swan) are omnibus wallets with no output-level
visibility, so they have no UTXOs — only on-chain (xpub/descriptor) wallets do.

## Data model (`utxos` table, `app/models.py::Utxo`)

One row per output paying one of the wallet's addresses:

- `txid`, `vout` — the outpoint (unique per wallet).
- `value_sats`, `address`, `script_type`, `chain` (0 receive / 1 change), `deriv_index`, `is_change`.
- `label_kind` — provenance snapshot of the owning account's label (e.g. `KYC` / `non-KYC`),
  refreshed each sync.
- `created_height`/`created_at`, and `spent_txid`/`spent_height`/`spent_at` (NULL ⇒ unspent).

The table is created by `Base.metadata.create_all` (no migration needed). Deleting a wallet or
account cascades its UTXOs (`Wallet.utxos` relationship).

## Scanning (`app/services/importers/xpub.py`)

`_scan_addresses` now emits a per-output UTXO inventory (`ScanResult.utxos`) alongside the
existing net-per-tx `ScanResult.txs`. Two passes over the wallet's touching transactions:

1. record every output paying one of our addresses (change is exact — derived from chain 1,
   not heuristically guessed);
2. mark any of our recorded outputs spent by a later input.

`persist_utxos` upserts by `(wallet_id, txid, vout)` — idempotent across re-syncs (refreshes
spent status + label, never duplicates). Called from `import_xpub`.

## Inventory & privacy lints (`app/services/coins.py`)

- `list_utxos(account_id, unspent_only=True)` — the live coin set.
- `privacy_warnings(account_id)`:
  1. **KYC / non-KYC merge** — a single spend that co-spent inputs with two or more distinct
     provenance labels publicly links those wallets (common-input-ownership). Examined across
     accounts (a merge can cross account boundaries).
  2. **Address reuse** — an address that received funds more than once is trivially clusterable.
  3. **Change in inventory** (info) — each unspent change output is already linked to the
     payment that created it; consolidating/spending with unrelated coins widens that link.

## UI

`GET /accounts/{id}/coins` (`app/templates/coins.html`, linked from the account detail page):
the unspent UTXO inventory plus the privacy panel. Outpoints/txids link to your configured
block explorer when set.

## Not in scope (would be a different project)

Anything pointed *outward* — clustering strangers, entity attribution, fund-flow tracing, risk
scoring — is deliberately excluded; see the scope discussion in the project notes. A future
Phase 3 (UTXO-anchored cost basis / specific-ID disposal selection) would touch the tax engine
and is tracked separately.
