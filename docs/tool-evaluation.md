# Tool Evaluation — rotki vs RP2+DaLI vs build-from-scratch

*Spike date: 2026-06-15. Lighter targeted research pass. No personal data used in any search.*
*Not tax advice — tax logic must be re-verified against current IRS guidance before relying on generated forms.*

## TL;DR recommendation (REVISED after round-2 research)

A round-2 scout (Clams, the Start9/Umbrel marketplaces, the crypto-tax landscape) surfaced a tool — **Clams** — that already does the single hardest thing we'd build (xpub/descriptor → on-chain transaction history → cost-basis events, fully local, against your own electrs/Esplora/Core). **Before committing to a from-scratch build, verify Clams hands-on.** The decision is now genuinely:

- **Path A — Package/adopt Clams** (it already exists and runs on your node today; not yet packaged for StartOS). Fastest to a working replacement for bitcoin.tax. Blockers to verify: license/source availability of the V2 Rust engine, its commercial tier, and whether it emits real Form 8949.
- **Path B — Build our own** (scratch + RP2 engine, Python). Full control, exact spec, but reimplements what Clams already does.

**Recommended next step: a short verification spike on Clams** (run it locally against synthetic/testnet data + confirm license + confirm 8949 output). Then pick Package vs Build with the riskiest unknowns retired.

*Original round-1 recommendation (still the fallback if we build): from-scratch Python app embedding RP2 (Apache-2.0), running RP2 once per account/wallet to get per-wallet FIFO + a Form 8949 per account — our segregation model sidesteps RP2's unfinished per-wallet feature (issue #135).*

## The decisive insight

The three tax-critical capabilities are **missing in both** off-the-shelf tools:

| Capability | rotki | RP2 + DaLI |
|---|---|---|
| **Bitcoin on-chain tx history → cost-basis events** | ❌ balances only (tx history "will fail", issue #2880) | ❌ no on-chain ingestion at all |
| **Per-wallet basis (Rev. Proc. 2024-28) + Form 8949** | ❌ universal-pool PnL CSV, no 8949 | ⚠️ real 8949/FIFO, but per-wallet not implemented (issue #135) |
| **True multi-user (per-person login)** | ❌ single-user-per-instance | ❌ single-user, file-driven |

So no matter the foundation, **we own**: xpub→transaction ingestion via electrs/Fulcrum, per-wallet basis handling, US form output, account/label model, and (later) multi-user. The question is only which tool saves the *most* of the *remaining* plumbing.

## Option scorecard

### RP2 + DaLI — adopt RP2 as the engine ✅ (recommended core)
- **License: Apache-2.0** — permissive; no copyleft on our UI/web code.
- **Pure Python** calc engine (CLI/library): input = transaction spreadsheet + INI config; output = **Form 8949-format** report + capital-gains/cost-basis/lot audit trail. FIFO/LIFO/HIFO.
- **Calculation runs fully offline** — good for local-only.
- **Per-wallet gap is largely neutralized by our design:** RP2 pools lots within a given input set, so feeding it **one account's transactions at a time** yields per-account (per-wallet) FIFO + a separate 8949 per account — exactly our segregation requirement. (Validate this approach against RP2 semantics during MVP.)
- **Gaps we own anyway:** no Strike/Swan/xpub/on-chain loaders in DaLI; no UI; no multi-user; offline *crypto* price source needs supplying (via CSV or a local price plugin).
- Refs: github.com/eprbell/rp2 · issue #135 (per-wallet) · github.com/eprbell/dali-rp2

### rotki — viable fork, but heavier and AGPL ⚠️
- **License: AGPLv3** — fork must stay open-source; network-service clause is compatible with per-user self-hosted StartOS but encumbers any closed extensions.
- **Strong plumbing:** runs **headless in Docker (no Electron)**, encrypted SQLCipher per-user DB, Vue/TS UI, read-only **Coinbase API**, **Bisq CSV**, **xpub watch-only** (P2PKH/P2SH-P2WPKH/WPKH) pointed at a **self-hosted Mempool** instance.
- **But:** **no BTC transaction history** (the riskiest unknown), **no Form 8949 / Schedule D**, **not** built around 2025 per-wallet rules, **single-user-per-instance**, and a large ~10-yr codebase to learn and partly fight (universal-pool tax engine). Public-node price fallback would need auditing for strict local-only.
- Refs: github.com/rotki/rotki · hub.docker.com/r/rotki/rotki · issue #2880 (BTC tx) · headless fork: github.com/nerevu/headless-rotki

### Build from scratch (with RP2 embedded) ✅
- Given moderate volume (<500 tx/yr — daily DCA plus other activity) and that the hard subsystems are net-new regardless, a small focused codebase is cleaner than bending a large one. We get a clean account/label + multi-user model, strict local-only control, and StartOS-shaped packaging from day one — while **not** reinventing US tax math (RP2 does it).

## StartOS packaging (Part A findings)

- **Format `.s9pk`**, current **TypeScript SDK `@start9labs/start-sdk`** (v1.5.3); StartOS core 0.4.x is beta. The TS SDK **replaces** the old `manifest.yaml`. Treat GitHub templates as ground truth over docs.
- **Python web service in Docker has direct precedent:** `Start9Labs/searxng-startos` (Python/Flask + caddy + valkey). Fork `hello-world-startos`, point at our Python image, fill in TS glue (interfaces, dependencies, health checks, backups, migrations).
- **Auth is single-admin** (one server password). **Multi-user must be built into our app** — confirmed.
- **Service-to-service** via stable hostname `<package-id>.startos:<port>`; precedent: **electrs → `bitcoind.startos`** authenticating via mounted `.cookie`, with health checks gating on IBD. Same path reaches Fulcrum/electrs/mempool/Core RPC.
- **Effort:** simple single-container ~a few days; with dependencies + config + migrations + our own auth, **1–2+ weeks** to a submittable package.

## Connector reality (Part B findings)

| Source | Pull method | Notes |
|---|---|---|
| **Coinbase** | **Read-only API** + CSV | App API OAuth scopes `wallet:transactions:read`, `wallet:buys:read`, `wallet:accounts:read`. App API is the right surface for a tax tool. |
| **Strike** | API (read scopes) **+ CSV (preferred)** | API is payments-oriented; CSV export (Dashboard → Activity → report) is the reliable tax-grade ledger. |
| **Swan** | **CSV only** | Public API is B2B partner on-ramp; individuals use full tx-history CSV + statements. |
| **Bisq** | **CSV only (local, by design)** | v1: Portfolio→History→Export. Bisq 2/Easy: all local, CSV export in UI. No remote API. |

**Architecture flag:** "local-only" is in tension with live exchange APIs. **Default to CSV import everywhere; gate Coinbase/Strike API connectors behind explicit per-use opt-in.** xpub on-chain (via electrs/Fulcrum) is the spine for all non-custodial activity.

## US tax law (Part C — all confirmed, high confidence)

- **Rev. Proc. 2024-28** — safe harbor to allocate unused basis per wallet/account as of **2025-01-01**; ends universal pooling. Per-wallet mandate (Treas. Reg. §1.1012-1(j)) applies to dispositions on/after Jan 1, 2025.
- **Notice 2025-7** — during 2025, specific identification of broker-custodied units may be made via own books/records or standing order (not communicated to broker at sale). **Extended into 2026 by Notice 2026-20.**
- **Form 1099-DA** — broker reporting from **2025** activity: **gross proceeds** (forms ~Feb 2026); **cost basis** phases in for 2026 transactions (filed 2027).
- **FIFO is the default** absent valid specific identification (made no later than the transaction); applied per-wallet from 2025-01-01.
- Sources: irs.gov/pub/irs-drop/rp-24-28.pdf · irs.gov/pub/irs-drop/n-25-07.pdf · irs.gov/instructions/i1099da

→ Your "FIFO by account since the start of 2025" understanding is **correct**. Re-verify specifics (and Notice 2026-20) with the deep-research pass before the tax engine is finalized.

---

# Round-2 research (prior art, marketplaces, landscape)

## Clams (clams.tech) — the pivotal finding

There are **two** Clams products; don't conflate them:
- **Clams Remote** — older CLN node-control UI (TS/Svelte). *Already packaged for StartOS* (`clams-tech/clams-remote-startos`). Not an accounting tool.
- **Clams (V2)** — *"purpose-built Bitcoin accounting,"* rebuilt in **Rust**: single binary = CLI + HTTP REST server, **local LMDB** storage. **This is the relevant one.**

What Clams V2 already does (overlaps our entire spec):
- ✅ **xpub / output-descriptor sync → on-chain tx history**, read-only (never needs keys) — *the hard part we lack.* Sources chain data from **Electrum (electrs/Fulcrum) / Esplora / Bitcoin Core RPC**, explicitly supports a Start9/Umbrel node.
- ✅ CLN + LND + multisig; double-entry accounting; **cost basis**; short/long-term holding periods; CSV import **and** export; transaction labeling.
- ✅ **Multi-user: workspaces + "books" + role-based permissions** — essentially the "uncle for the family" feature, already built.
- ✅ Local-first, single-service, reverse-proxy-frontable — **architecturally ideal for StartOS**.

Open questions (WebFetch was denied; verify directly):
- ⚠️ **License/source of the V2 Rust binary unconfirmed.** Older Clams repos are open (GPL/MIT/Apache); the V2 engine may be source-available with a **commercial tier (free under $1M revenue)**. May not be permissively forkable/redistributable.
- ⚠️ **Rust**, not Python — can't vendor into a Python app; customization means Rust or upstream PRs.
- ⚠️ Unconfirmed whether it emits actual **Form 8949 / Schedule D** vs. generic gain/loss reports; selectable methods (FIFO/HIFO/spec-ID) unconfirmed.
- ⚠️ V2 accounting engine **not yet packaged for StartOS or Umbrel** — a real gap/opportunity.

Refs: clams.tech · clams.tech/for-individuals · clams.tech/server · github.com/clams-tech · clams.tech/blog/sparrow-wallet-bitcoin-accounting-setup-guide

### Clams verification pass (2026-06-15, fetched from clams.tech directly)

**Decisive findings — Clams is NOT a viable foundation for a sovereign StartOS app:**
- ❌ **Closed source (confirmed).** FAQ, verbatim: *"No, it isn't [open source]. We are major proponents of open source… but we want to build Clams into a long-term sustainable business, and… open sourcing it at this time does not contribute to that."* Only peripheral repos (Remote=GPL-3.0, docs=Apache-2.0, decoders=MIT) are open; the V2 accounting engine ships as **binaries only** (`clams-tech/releases`). → Can't audit, fork, or cleanly/legally repackage it.
- ⚠️ **Requires a Clams account login.** *"You just need an email address to sign in via magic link. You can also use Google or Apple SSO."* There is a dedicated **Clams Cloud** product (clams.tech/cloud). The promo video shows logging into a Clams account to get operational. → Even with local data storage, it depends on **Clams' auth servers** — an outbound, third-party dependency at odds with "nothing leaves the machine" and the StartOS sovereignty model. (Residual uncertainty: not 100% verbatim-confirmed whether the bare CLI can run with no login; marketing + promo video strongly imply login is required. Settle by trialing it or checking docs.clams.tech.)
- **Commercial license:** free for personal use & businesses <$1M revenue; enterprise license above.
- ✅ Data-locality claim holds (*"Your data stays on your machine. Period."*) and three deploy modes exist: **Local** (your machine) / **Self-Hosted** (your servers) / **Hosted by Clams** (cloud).

**Verdict:** Clams is an impressive, local-data Bitcoin accounting product and a **strong reference design / competitive baseline** — but **closed-source + mandatory account login + commercial license** rule it out as something to **fork, package for StartOS, or adopt as a sovereign foundation.** It's the opposite of what a StartOS audience expects (open, no accounts, no phone-home). → Reinforces **building our own**, treating Clams' feature set (xpub/descriptor sync, workspaces/books, double-entry, holding-period tracking) as the bar to match.

Sources: clams.tech/faq · clams.tech/deployments · clams.tech/for-individuals · clams.tech/cli · clams.tech/cloud · github.com/clams-tech

## Marketplace prior art & templates

- **Umbrel** already has: **rotki**, **Toshi Moto** (xpub watch-only), **Ghostfolio**, **BTC Tracker**.
- **StartOS marketplace: no tax/accounting/portfolio app found** (verify directly) → genuine gap.
- **Best StartOS templates to study, in order:** `searxng-startos` (Python web-service wrapper mechanics) → `electrs-startos` (declaring a bitcoind dependency, cookie auth) → `mempool-startos` (web UI + backend + DB + *optional* electrs backend — closest to our shape) → rotki (domain/tax logic).
- **Dual-target Umbrel + StartOS is low-effort** with a **SQLite single-container** design (StartOS forbids docker-compose; Umbrel allows it). Keep a platform-agnostic Python core + two thin wrapper repos.

## Competitive landscape & differentiators

- Other open-source/local tools beyond rotki/RP2: **Cryptotithe**, **bitcoin-tax-tools** (Codes4Fun), **BittyTax** (UK).
- Commercial SaaS band: **$49–$389/yr** for normal users (CoinLedger, ZenLedger, CoinTracking, CoinTracker, Awaken), scaling to thousands. All cloud, ingest full history + PII. → a **free, local, unlimited-tx, Bitcoin-only** tool is a strong value wedge.
- **Table-stakes feature set** the market expects: Form 8949 + Schedule D + 1040 income; FIFO/LIFO/HIFO/Spec-ID; API auto-sync **+** CSV fallback; error/reconciliation tooling; audit-trail reporting; (nice-to-have) TurboTax export.
- **Confirmed differentiators / gaps to target:** per-wallet basis as a **first-class data model** (not a bolt-on) with 2024-28 safe-harbor allocation; documented **specific-ID lot selection**; **local-only privacy**; **Bitcoin-only xpub workflow** (a niche SaaS ignores); **basis reconstruction** (1099-DA gives gross proceeds but **no cost basis for 2025** — taxpayer must supply it on 8949).

Sources: apps.umbrel.com (rotki/toshi-moto/ghostfolio/btctracker) · github.com/Start9Labs (searxng/electrs/mempool -startos) · github.com/getumbrel/umbrel-apps · docs.start9.com/packaging/0.4.0.x · github.com/eprbell/rp2 · github.com/Codes4Fun/bitcoin-tax-tools

## Proposed MVP slice (next phase, on approval)

1. Project skeleton: Python service + SQLite, Dockerized, runs on Windows via WSL2.
2. Account/label model (single login to start).
3. **xpub import** → derive addresses → fetch tx history from electrs/Fulcrum (testnet/signet + synthetic fixtures first).
4. **CSV import** for one source (Swan or Coinbase) with synthetic fixtures.
5. Local historical price lookup (cost basis) — local CSV/cache, no PII outbound.
6. Embed **RP2**, run per-account → generate **Form 8949** draft for one account.
7. Minimal web UI to view per-account/per-wallet cost basis.

Everything tested with synthetic/testnet data; real data only ever loaded locally by the user, never piped into agent output.
