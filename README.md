# ArcaSats ₿

**Track the Chain. Own the Future.**

*(package/repo dir: `bitcoin-tax-tracker`)*

A **local-only** US Bitcoin tax & accounting tool, intended to run as a self-hosted service on **StartOS** (Start9) alongside an existing Bitcoin full node / Lightning / mempool / Electrum-server stack. Goal: replace a bitcoin.tax subscription with a private, self-hosted alternative.

> Status: **Pre-1.0 — feature-complete core, hardening before public release.** Implemented: accounts/manual entry, CSV import (Coinbase/Strike/Swan/Bisq/generic), xpub on-chain sync with buy/sell classification (incl. **multisig output-descriptor import**), FIFO/LIFO/HIFO **per-account** cost basis, **multi-source 15-minute USD pricing** (Coinbase/Bitstamp/your own mempool), block-explorer links, Form 8949/Schedule D, an optional **local** LLM "Ask your data" assistant, dark mode, and StartOS/Umbrel packaging scaffolds. Built from scratch (see decision below). **Direct exchange-API sync was removed in favor of CSV import** (keeps keys/traffic off third-party APIs). **202 tests passing.** A pre-release audit ([`docs/code-review.md`](docs/code-review.md)) tracks remaining work — treat current output as alpha and verify before filing.

## License & auditing

ArcaSats is free, open-source software released under the **[MIT License](LICENSE)** — you may use, modify, distribute, and sell it, including commercially; just keep the copyright notice. Copyright © 2026 Rapscallion.

This is privacy- and money-sensitive software, so **independent auditing is explicitly welcomed.** Start with [`SECURITY.md`](SECURITY.md) for the trust model and how to report issues, and [`docs/code-review.md`](docs/code-review.md) for an architecture + audit guide. This tool produces tax figures for your review — **it is not tax advice**; verify against current IRS guidance and consult a professional before filing.

## What it does (target)

- Import non-custodial wallet activity via **xpub/ypub/zpub** (watch-only), scanned through a local **electrs/Fulcrum** Electrum server on the node — no third-party explorers.
- Import custodial activity via **CSV export** (Coinbase, Strike, Swan, Bisq, or a generic format).
- Segregate coins into named **accounts / sub-accounts / labels** (KYC vs non-KYC, or per-person). **Single-user instance** (one instance per person); an optional app-wide password lock (`BTT_APP_PASSWORD`) can gate the whole app.
- Compute **cost basis per account** (FIFO/LIFO/HIFO), with an **informational** per-wallet breakdown. Tax reports are computed at the account level; per-wallet lot accounting is not yet a separate tax engine (see status/audit).
- Produce **US tax forms** — Form 8949, Schedule D (+ income schedules) — using **per-account** lot accounting. Rev. Proc. 2024-28 (effective 2025-01-01) allows per-account allocation via its safe harbor; if you need strict per-wallet lots, use one account per wallet/exchange location for now. Verify against current IRS guidance.

## Hard constraints

- **Local-only.** No personal/transactional data leaves the machine. Outbound is limited to: your own node's Electrum server (over Tor), and a market-price feed (public BTC/USD candles, batched by **week** — reveals only the week of activity, never amounts, addresses, or PII).
- **Read/download only** for any connected account — never transact.
- Node access for xpub scanning via **electrs/Fulcrum** (confirmed available).

## Decisions

| Question | Decision |
|---|---|
| Jurisdiction | US only (for now) |
| **Foundation** | **Build from scratch.** Evaluated rotki, RP2+DaLI, and Clams (see `docs/tool-evaluation.md`). Clams rejected: closed-source + mandatory account login. RP2 to be embedded later as the tax-calc engine. |
| Build approach | MVP first, incremental |
| Account model | Single-user instance; accounts/labels for segregation (one instance per person) |
| Node access | electrs / Fulcrum (Electrum server) |
| Sources | Coinbase (CSV), Strike (CSV), Swan (CSV), Bisq (CSV), generic (CSV), xpub (on-chain). *Direct exchange APIs removed — CSV only.* |
| Scale | Moderate — daily DCA plus other activity, mostly buys/transfers, few sells, <500 tx/year |
| **UI stack** | **FastAPI + HTMX + Tailwind** (Python end-to-end, single container) |
| UI palette | Adapted from dirigobtc.org — see `docs/design-system.md` |

## Build order

0. ✅ **Skeleton + look** — FastAPI app, styled dashboard shell, dark mode.
1. ✅ **Accounts/labels + manual transaction entry** (SQLite model).
2. ✅ **CSV import** (Coinbase/Strike/Swan/Bisq/generic) with synthetic fixtures.
3. ✅ **xpub import** → pure-Python derivation → tx history via electrs/Fulcrum (Tor-aware). *Riskiest piece — done.*
4. ✅ **Pricing + per-account cost basis** (informational per-wallet view) — FIFO/LIFO/HIFO engine, local price cache.
5. ✅ **Tax engine** — Form 8949 + Schedule D, per year, CSV export (own FIFO engine, not RP2 — see note).
6. ➖ **Read-only API connectors** — removed; superseded by CSV import (keeps keys/traffic off third-party APIs).
7. ➖ **Multi-user — removed.** Single-user instance; an optional single-password lock (`BTT_APP_PASSWORD`) gates the app when exposed. One instance per person.
8. ✅ **Packaging** — `startos/` (.s9pk scaffold) + `umbrel/` wrapper; vendored assets (no CDN).

> **Phase 5 note:** the prior plan was to embed RP2 as the tax engine. For a Bitcoin-only,
> low-volume, per-account use case, a focused in-house FIFO engine (`app/services/costbasis.py`)
> proved cleaner and avoids RP2's unfinished per-wallet support + ODS round-trip. RP2 remains a
> good optional cross-check later.

## Install on Windows (download & run)

For a normal install (no developer setup needed):

1. **Install Python** (one-time) — get **Python 3.12 or newer** from
   <https://www.python.org/downloads/> and tick **“Add python.exe to PATH”** in the installer.
2. **Download** the latest **source zip** from the
   [Releases page](https://github.com/rapscallion530/ArcaSats/releases) (the
   `Source code (zip)` asset), and **extract** it anywhere (e.g. your Documents folder).
3. **Run** — open the extracted folder and **double-click `run.bat`**. The first launch creates a
   local virtual environment and installs dependencies (one-time, ~30s), then **opens ArcaSats in
   its own window** — no terminal to keep open. **Close the window to quit.** Later launches start
   instantly. *(If a native window isn't available on your system, it falls back to opening
   <http://127.0.0.1:8000> in your browser.)*
4. **Add your data** — in the app: **Settings** to point at your own node / pick a price source,
   then **Accounts → import a CSV** (Coinbase/Strike/Swan/Bisq) or **add an xpub** to sync on-chain
   activity. Enter your actual prices where you know them (they always win over estimates).

**`.onion` works out of the box.** The desktop app runs its **own** Tor in the background (the
official Tor binary, downloaded + checksum-verified on first use, cached under `data\tor\`), so a
`.onion` node/mempool connects with **no Tor Browser and no separate Tor service**. Keep it patched
from **Settings → Tor (built in) → Update**, or `python scripts/update_tor.py`. (On a server/StartOS,
ArcaSats uses the system Tor instead.)

**Your data stays on your machine.** It’s written to a `data\` folder next to the app
(`data\btt.sqlite` + `secret.key`) — **never** committed or uploaded. The only outbound traffic is
to *your own* node (Electrum, optionally over Tor) and, if enabled, a public BTC/USD price feed
(weekly candle windows — reveals only the week of activity, never amounts/addresses/PII). To
**back up** or **move** to another machine, copy the `data\` folder. To **stop** the app, just
**close the ArcaSats window**. *Tax figures are drafts for your review — not tax advice.*

## Run it locally (Windows, Python 3.x)

**One click:** double-click `run.bat` (first run sets up the venv + deps, then opens ArcaSats in
its own window; close the window to quit).

Or manually:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload   # headless server (browser tab)
```

Then open <http://127.0.0.1:8000>. Health check: <http://127.0.0.1:8000/health>.

For the **native-window** experience (no console/tab): `pip install -r requirements-desktop.txt`
then `.\.venv\Scripts\pythonw.exe desktop.py`. The headless `uvicorn app.main:app` path
(server / StartOS / Docker) is unchanged and never needs the desktop dependency.

## Windows now → StartOS later (same code)

This is a platform-agnostic Python app. Run it on Windows today; package the *same*
code into a StartOS `.s9pk` (or Umbrel app) later — no rewrite. **Data is portable:**
everything lives under the data dir (`data/btt.sqlite` + `secret.key`). To migrate,
copy that file into the packaged service's `/data` volume and the accounts, wallets,
transactions, and tax history carry over.

## Connecting to your node (xpub sync)

Set the Electrum server via env vars (electrs/Fulcrum):

```
BTT_ELECTRUM_HOST=<host>     # e.g. a Tailscale hostname/IP of your StartOS box
BTT_ELECTRUM_PORT=50001
```

- **Tailscale** is the easiest LAN-style path from a laptop to a StartOS box — reach electrs
  directly on `:50001`, no Tor. For a `.onion` host the app auto-routes via the Tor SOCKS proxy.
- On Windows, put these in a gitignored **`env.local.ps1`** (`$env:BTT_ELECTRUM_HOST = "..."`);
  `run.ps1` sources it automatically, so `run.bat` launches fully wired.
- Validated against real `electrs` (verbose `get_transaction` supported). Use
  `python scripts/check_node.py <host> <port>` to diagnose connectivity (uses the public
  BIP84 test xpub — no personal data).

Or via Docker:

```bash
docker build -t bitcoin-tax-tracker .
docker run --rm -p 8000:8000 bitcoin-tax-tracker
```

## Project layout

```
app/
  main.py                  # FastAPI app + routes
  templates/               # Jinja2 (base, dashboard, partials/)
  static/tokens.css        # design tokens (dirigo palette)
requirements.txt
Dockerfile                 # single-container; for StartOS/Umbrel later
docs/
  tool-evaluation.md       # rotki vs RP2 vs Clams research + decision
  design-system.md         # palette + design tokens
```

## Assets & persistence (resolved)

- **Assets are vendored and local by default** (`BTT_ASSETS=local`): `app/static/tailwind.css`
  (built from `input.css`) and `app/static/vendor/htmx.min.js` — an ordinary launch makes **no
  external requests**. `BTT_ASSETS=cdn` is an opt-in dev convenience only.
- **Data persists** in SQLite at `data/btt.sqlite` (WAL mode).

For the remaining pre-release work, see [`docs/code-review.md`](docs/code-review.md).

## Testing & privacy

- Dev/test on Windows via local `uvicorn` or Docker; package as `.s9pk` only at deploy.
- All development uses **synthetic / testnet fixtures** (fake xpubs, randomized CSVs). Real data is only ever loaded by the user, locally, and never piped into agent/tool output.

## Disclaimer

Not tax advice. Tax logic must be verified against current IRS guidance before relying on any generated forms.
