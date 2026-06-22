# ArcaSats v0.1.0 — first public alpha

Local-only, self-hosted US Bitcoin tax & accounting. Runs entirely on your own
machine against your own node — watch-only, no private keys, no third-party API traffic.

## Features
- Accounts + manual entry; CSV import (Coinbase, Strike, Swan, Bisq, generic)
- xpub on-chain sync with buy/sell classification, plus **multisig output-descriptor
  import** (Sparrow/Unchained `wsh/sh/sh(wsh)` of `(sorted)multi`)
- FIFO / LIFO / HIFO **per-account** cost basis (Rev. Proc. 2024-28), cross-wallet
  transfer detection + basis carry
- **Multi-source 15-minute USD pricing** (Coinbase / Bitstamp / your own mempool);
  enter your actual price paid to override the spot estimate
- Form 8949 + Schedule D, gift-basis statement, audit / "explain this gain" view
- Multi-user (owner-scoped), optional **local** LLM "Ask your data" assistant (loopback-only)
- BTC ⇄ sats toggle, block-explorer links, dark mode; StartOS/Umbrel packaging scaffolds

## Privacy & security
- Loopback bind by default; Tor for node access; vendored assets (zero external requests)
- Three independent pre-release audits applied; MIT licensed; see `SECURITY.md`
- 120 tests; CI on Python 3.12 & 3.13

## Status & known limits
Alpha. **Not tax advice — verify against current IRS guidance and consult a professional
before filing.** Documented post-launch work in `docs/code-review.md`: on-chain miner-fee
basis treatment, Taproot derivation, login rate-limiting, Alembic migrations, UX-safety extras.
