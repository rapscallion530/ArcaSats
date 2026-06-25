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

## KYC provenance through the cost-basis engine (Layer A + B — BUILT)

KYC-ness is an **acquisition** property, so it lives on the lot, not the UTXO (this also covers
custodial omnibus sources with no output-level visibility). See `app/services/costbasis.py`:

- **Layer A — KYC on the lot.** The acquiring account's `label_kind` is snapshotted onto each
  buy/income/opening (`Transaction.kyc_origin`, mirroring `Utxo.label_kind`). A **shared-txid**
  self-transfer carries provenance by storing the consumed **source-lot fragments** as JSON on the
  destination `transfer_in` (`Transaction.carried_lots`); `compute()` rebuilds the destination lots
  from those fragments, preserving each one's **original acquisition date** (holding period tacks,
  IRC §1223) and its own KYC label. A **fuzzy** link (no shared txid) is not fragment-carried —
  a hop through an intermediary we can't prove is yours is a final break of ownership (by default
  no carry; confirming the address-matched pair in the inbox still carries the basis coarsely as
  one lot, without tacking the holding period).
  `CostBasisResult.holding_by_kyc` / `realized_by_kyc` (and the 8949 / assistant snapshot) report
  holdings + gains by class.
- **Layer B (start) — dispose by KYC status.** `Account.disposal_priority`
  (`non_kyc_first`/`kyc_first`) consumes the preferred KYC class first; within a class the
  account's FIFO/LIFO/HIFO ordering applies. Specific identification **by class**, no UTXO link
  required; the gain math is unchanged.

## Address-based fuzzy-hop detection (reconciliation inbox)

A self-transfer that routes through an address ArcaSats doesn't track (known → unknown → known)
has two on-chain txs with *different* txids, so the shared-txid auto-reconciler can't link them.
The reconciliation inbox proposes these for the user to confirm. Detection is **address-first**:
the xpub scanner records, per our transaction, the foreign address one hop away — a spend's
**destination** (`HopAddress` direction `"out"`, free: it's a vout we already fetch) and an
inflow's **funder** (`"in"`, fetched from each input's previous tx, since a vin carries only
`txid:vout`). `costbasis.suggest_transfers` then matches an outflow whose destination address
later **funds** an inflow — the same unknown intermediary — regardless of how much the sats or the
timing drifted. There is **no amount+date fallback** (it mispairs across fees and long gaps and
was removed): an outflow with no shared address simply isn't suggested. The auto reconciler
(`reconcile_internal_transfers`) likewise carries only proven shared-txid transfers.

This stays **inward**: we only ever look at addresses *directly adjacent* to the user's own coins
(one hop), never a deeper walk through third-party addresses (that would be the out-of-scope
outward tracing). The captured addresses are local-only and never egressed. A confirmed match
carries basis **coarsely** (single lot) — a fuzzy hop never gets the precise shared-txid fragment
rebuild, and is a break by default until the user vouches for it.

## Phase 3 (deferred) — UTXO-outpoint specific identification

Disposing of *literal* chosen coins (not just a KYC class) is the only part that genuinely needs
UTXO granularity: a lot↔UTXO link (only on-chain receives have UTXOs; custodial lots are omnibus
with none) plus a per-disposal coin-picker UI. When that lands, a UTXO derived from **mixed
inputs** (a consolidation co-spending KYC + non-KYC coins into one output) is labeled **KYC** —
the conservative choice (`costbasis._merge_kyc`): once commingled, the surveillance taint already
spreads, so labeling the result KYC reflects the true exposure rather than overstating "clean"
holdings.

## Not in scope (would be a different project)

Anything pointed *outward* — clustering strangers, entity attribution, fund-flow tracing, risk
scoring — is deliberately excluded; see the scope discussion in the project notes.
