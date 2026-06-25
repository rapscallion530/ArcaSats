# Single-container image. Works for local dev, Umbrel, and (wrapped) StartOS.
# Packaged builds compile Tailwind to a static CSS and run with vendored assets
# (BTT_ASSETS=local) so the UI makes NO external requests.

# --- asset build stage: compile Tailwind CSS with the standalone CLI (no Node) ---
FROM debian:bookworm-slim AS assets
ARG TAILWIND_VERSION=v3.4.17
ARG TARGETARCH=amd64
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY tailwind.config.js ./
COPY app ./app
# SHA-256 of the official standalone binaries — PINNED to TAILWIND_VERSION (v3.4.17). On a
# version bump, refresh these from that release's sha256sums.txt or the build will (correctly)
# fail the integrity check.
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) TW=tailwindcss-linux-x64;  SHA=7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4 ;; \
      arm64) TW=tailwindcss-linux-arm64; SHA=69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -sSL "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${TW}" -o /usr/local/bin/tailwindcss; \
    echo "${SHA}  /usr/local/bin/tailwindcss" | sha256sum -c -; \
    chmod +x /usr/local/bin/tailwindcss; \
    tailwindcss -c tailwind.config.js -i app/static/input.css -o app/static/tailwind.css --minify

# --- runtime ---
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Bring in the compiled CSS from the asset stage
COPY --from=assets /build/app/static/tailwind.css ./app/static/tailwind.css

ENV BTT_ASSETS=local
ENV BTT_DATA_DIR=/data
# Make the app's exposure guard aware that the container binds all interfaces. A bare
# `docker run` exposed to a network then REFUSES to start unless the operator sets
# BTT_APP_PASSWORD (the in-app password lock) or BTT_ALLOW_OPEN_EXPOSURE=1 (only when a
# trusted authenticated gateway — e.g. StartOS/Umbrel — fronts this port).
ENV BTT_BIND_HOST=0.0.0.0
EXPOSE 8000
VOLUME ["/data"]

# Drop privileges: run as a non-root system user and make the data volume writable by it.
# (A bind-mounted /data must be writable by uid 10001 on the host, or pass --user.)
RUN useradd --system --no-create-home --uid 10001 arca \
    && mkdir -p /data && chown -R arca:arca /data /app
USER arca

CMD ["sh", "-c", "uvicorn app.main:app --host \"${BTT_BIND_HOST:-0.0.0.0}\" --port 8000"]
