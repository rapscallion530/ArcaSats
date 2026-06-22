# Security & Privacy — ArcaSats

ArcaSats handles privacy- and money-sensitive data (extended public keys, transaction
history, cost basis). Independent review is welcomed. This document describes the trust
model so auditors know what to scrutinize, and how to report issues.

## Threat model & design goals

- **Local-only by default.** All user data lives in a single SQLite file (`data/btt.sqlite`)
  on the user's machine. The app binds to `127.0.0.1` by default.
- **Watch-only.** ArcaSats never asks for, stores, or handles private keys or seed phrases.
  It ingests extended **public** keys (xpub/ypub/zpub) and addresses. The worst-case
  compromise is a **privacy** loss (someone learning your addresses/balances), not theft.
- **Minimal, auditable egress.** The only outbound network actions are:
  1. Queries to the user's **own** Electrum server (electrs/Fulcrum), preferably over Tor
     (`.onion`). The xpub string is never transmitted — only derived script hashes.
  2. A public **BTC/USD price** feed (Coinbase Exchange candles) — only dates/times are
     sent, never amounts, addresses, or balances. Gated by `BTT_ENABLE_NETWORK`.
  3. An **optional local LLM** endpoint the user configures (off by default). The assistant
     talks ONLY to a model on **this machine (loopback)** by default; HTTP redirects are
     blocked and the locality check resolves hostnames (no `.internal`→public bypass). A model
     elsewhere on your private LAN requires the explicit `BTT_ASSISTANT_ALLOW_LAN=1` opt-in;
     public endpoints are always refused.
  Every intentional outbound action is recorded in the in-app **Outbound Data Log**
  (host + purpose only — never coin/PII data).

## What an auditor should focus on

- **Egress boundary:** confirm no addresses/xpubs/amounts/PII leave the machine. Key spots:
  `app/services/electrum.py` (scripthash queries), `app/services/pricing.py` (date-only
  fetches), `app/services/llm.py` (`is_local()` gate), `app/services/outbound.py`.
- **Access control:** in secured (multi-user) mode, owner-scoping must prevent one user from
  reading another's accounts. Review `app/services/auth.py`, the auth middleware in
  `app/main.py`, and `can_access` / `list_accounts(user_id, role)` usage across routers.
- **Cost-basis correctness:** `app/services/costbasis.py` (FIFO/LIFO/HIFO, lot consumption,
  transfer/gift basis carry). Tax figures must be deterministic and reproducible.
- **Crypto:** the pure-Python BIP32/secp256k1 in `app/services/bip32.py` and address/script
  derivation in `app/services/script.py` (validated against BIP32/BIP84/BIP173 test vectors
  in `tests/`).

See [`docs/code-review.md`](docs/code-review.md) for a fuller architecture + audit guide.

## Reporting a vulnerability

Please report security issues **privately** rather than opening a public issue:

- Open a GitHub **private security advisory** (Security → Advisories → Report a vulnerability)
  once the project is on GitHub, or
- Contact the maintainers through the channel listed in the repository's profile.

Include reproduction steps and the affected file(s)/version. We aim to acknowledge reports
promptly and will credit reporters who wish to be named.

## Not tax or financial advice

ArcaSats organizes records and computes figures for your review. It is **not tax advice**.
Verify all output against current IRS guidance and consult a professional before filing.
