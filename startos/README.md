# StartOS packaging (`.s9pk`)

This app is a single Python container (SQLite, local data dir, web UI on `:8000`).
StartOS 0.4.x packages are built with the **TypeScript SDK** (`@start9labs/start-sdk`)
and produce a signed `.s9pk`. Treat the official **hello-world** template as ground
truth — the SDK surface evolves faster than docs.

## Build steps

1. Install the toolchain: `start-cli` + Docker/Podman (see docs.start9.com/packaging/0.4.0.x).
2. Scaffold from the template, then drop our service in:
   ```bash
   git clone https://github.com/Start9Labs/hello-world-startos
   cd hello-world-startos
   # set the image to our Dockerfile build, copy manifest.ts below, adjust interfaces/deps
   make            # builds bitcoin-tax-tracker.s9pk
   ```
3. Sideload: `start-cli package install bitcoin-tax-tracker.s9pk` (or via the StartOS UI).

## Service shape

- **Image:** our root `Dockerfile` (sets `BTT_ASSETS=local`, `BTT_DATA_DIR=/data`).
- **Data:** persistent volume mounted at `/data` (SQLite + `secret.key`).
- **Interface:** one `ui` interface on port 8000 → StartOS exposes LAN HTTPS + Tor `.onion`.
- **Auth:** StartOS is single-admin; this app implements its OWN multi-user mode
  (`/setup` → admin → members). Leave it in "open mode" for single-user.
- **Dependencies:** electrs (Electrum) for xpub sync, reached at the in-StartOS hostname.
  Set `BTT_ELECTRUM_HOST=electrs.startos` (or the installed electrs package id) and
  `BTT_ELECTRUM_PORT=50001`. For a `.onion` Electrum host, the app routes via Tor
  automatically (`BTT_TOR_SOCKS_HOST/PORT`).

## manifest.ts (sketch — adapt to the current SDK)

See `manifest.ts` in this folder. Key fields: `id: "bitcoin-tax-tracker"`, `title`,
volume `data`, a `ui` interface on 8000, and an optional dependency on `electrs`.

## Privacy notes

- `BTT_ENABLE_NETWORK` stays `0` by default (no outbound exchange-API calls, no online
  price fetch). The UI uses vendored assets (`BTT_ASSETS=local`) — no CDN requests.
- The only outbound traffic when configured: read-only Electrum queries to the user's
  own node, and (only if the user opts in) exchange APIs / price feed.
