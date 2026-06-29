# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Bundled, self-managing Tor — so the desktop app reaches a `.onion` node with no second app
(no Tor Browser, no separately-installed daemon).

On the desktop launch (`desktop.py`, gated by BTT_MANAGED_TOR) ArcaSats runs its OWN Tor instance:
it resolves a `tor` binary (env / a cached download / one already on PATH), downloading the official
Tor Expert Bundle over HTTPS + verifying its sha256 the first time, then launches Tor on an
ephemeral, loopback-only SocksPort with an isolated DataDirectory and stops it on exit. The rest of
the app routes `.onion` traffic through `active_proxy()`.

The server / StartOS deployment never enables this (BTT_MANAGED_TOR off) and keeps using the system
Tor. Everything here fails soft: any download/launch problem is logged and leaves the app working
against whatever Tor proxy is configured manually.

Update / CVE story: the installed version + hash are recorded in manifest.json; `check_update()`
compares against the latest published version and `update()` re-downloads+verifies a newer build.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.request

from app import config

# --- locations ---------------------------------------------------------------
TOR_DIR = config.DATA_DIR / "tor"
BIN_DIR = TOR_DIR / "bin"
DATA_SUBDIR = TOR_DIR / "data"
TORRC = TOR_DIR / "torrc"
LOG = TOR_DIR / "tor.log"
MANIFEST = TOR_DIR / "manifest.json"
SERVICE_LOG = TOR_DIR / "service.log"

# Official Tor download endpoints (HTTPS). The expert bundle is versioned by the Tor Browser
# release; the latest version is discovered from the update JSON (overridable via BTT_TOR_VERSION).
_DOWNLOADS_JSON = "https://aus1.torproject.org/torbrowser/update_3/release/downloads.json"
_ARCHIVE = "https://archive.torproject.org/tor-package-archive/torbrowser"

_WINDOWS = os.name == "nt"

# --- module state (single managed instance) ----------------------------------
_proc: subprocess.Popen | None = None
_socks_port: int | None = None
_bootstrapped: bool = False


def managed_enabled() -> bool:
    """Managed Tor only runs when explicitly turned on (desktop.py sets this). Off => the headless
    server / StartOS path, which uses the system Tor."""
    return os.environ.get("BTT_MANAGED_TOR", "0") == "1"


def _log(msg: str) -> None:
    try:
        TOR_DIR.mkdir(parents=True, exist_ok=True)
        with open(SERVICE_LOG, "a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:  # noqa: BLE001 — logging must never break the app
        pass


# --- binary resolution -------------------------------------------------------
def _binary_name() -> str:
    return "tor.exe" if _WINDOWS else "tor"


def resolve_binary() -> str | None:
    """First usable tor binary: explicit override, then our cached download, then PATH."""
    override = os.environ.get("BTT_TOR_BINARY", "").strip()
    if override and os.path.isfile(override):
        return override
    cached = BIN_DIR / _binary_name()
    if cached.is_file():
        return str(cached)
    found = shutil.which("tor")
    return found or None


# --- platform / version ------------------------------------------------------
def _platform_tag() -> tuple[str, str]:
    sysname = platform.system().lower()
    ostag = {"windows": "windows", "linux": "linux", "darwin": "macos"}.get(sysname, sysname)
    mach = platform.machine().lower()
    arch = "aarch64" if mach in ("arm64", "aarch64") else "x86_64"
    return ostag, arch


def latest_version() -> str | None:
    """Discover the latest Tor (Browser) version string; None on any failure."""
    pinned = os.environ.get("BTT_TOR_VERSION", "").strip()
    if pinned:
        return pinned
    try:
        body = _http_text(_DOWNLOADS_JSON)
        return (json.loads(body).get("version") or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        _log(f"latest_version failed: {exc!r}")
        return None


def installed_version() -> str | None:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8")).get("version")
    except Exception:  # noqa: BLE001
        return None


def _version_newer(latest: str, current: str | None) -> bool:
    """True if `latest` is a newer dotted version than `current` (missing current => True)."""
    if not current:
        return True

    def parts(v: str) -> list[int]:
        out = []
        for chunk in v.replace("-", ".").split("."):
            out.append(int(chunk) if chunk.isdigit() else 0)
        return out

    return parts(latest) > parts(current)


# --- HTTP helpers (clearnet HTTPS; the binary download isn't sensitive) -------
def _http_bytes(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ArcaSats"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (HTTPS official host)
        return resp.read()


def _http_text(url: str, timeout: float = 30.0) -> str:
    return _http_bytes(url, timeout).decode("utf-8", "replace")


def _sum_for(sums_text: str, filename: str) -> str | None:
    for line in sums_text.splitlines():
        bits = line.split()
        if len(bits) == 2 and bits[1].lstrip("*").endswith(filename):
            return bits[0].lower()
    return None


# --- download + verify -------------------------------------------------------
def download(version: str | None = None) -> bool:
    """Download + sha256-verify the Tor Expert Bundle and extract its `tor/` payload into BIN_DIR.
    Records manifest.json. Returns True on success; logs + returns False on any failure."""
    version = version or latest_version()
    if not version:
        _log("download: could not determine a version")
        return False
    ostag, arch = _platform_tag()
    fname = f"tor-expert-bundle-{ostag}-{arch}-{version}.tar.gz"
    url = f"{_ARCHIVE}/{version}/{fname}"
    try:
        blob = _http_bytes(url)
        digest = hashlib.sha256(blob).hexdigest()
        sums = _http_text(f"{_ARCHIVE}/{version}/sha256sums-signed-build.txt")
        expected = _sum_for(sums, fname)
        if not expected:
            _log(f"download: no checksum entry for {fname}")
            return False
        if expected != digest:
            _log(f"download: SHA256 MISMATCH for {fname} (refusing)")
            return False
        _extract(blob)
        TOR_DIR.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps({"version": version, "sha256": digest}), encoding="utf-8")
        _log(f"download: installed Tor {version} ({fname})")
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"download failed for {url}: {exc!r}")
        return False


def _extract(blob: bytes) -> None:
    """Extract members under `tor/` into BIN_DIR (flattening the prefix)."""
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.startswith("tor/"):
                continue
            rel = m.name[len("tor/"):]
            if not rel or ".." in rel.split("/"):   # path-traversal guard
                continue
            dest = BIN_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            if not _WINDOWS:
                os.chmod(dest, 0o755)


def ensure_binary() -> str | None:
    """A usable tor binary, downloading it once if we don't already have one."""
    found = resolve_binary()
    if found:
        return found
    if download():
        return resolve_binary()
    return None


# --- launch / bootstrap ------------------------------------------------------
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_torrc(socks_port: int) -> str:
    """torrc for a loopback-only client instance with an isolated data dir."""
    return (
        f"SocksPort 127.0.0.1:{socks_port}\n"
        "SocksPolicy accept 127.0.0.1\n"
        "SocksPolicy reject *\n"
        "ClientOnly 1\n"
        f"DataDirectory {DATA_SUBDIR}\n"
        f"Log notice file {LOG}\n"
    )


def _log_bootstrapped() -> bool:
    try:
        return "Bootstrapped 100%" in LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def wait_bootstrap(timeout: float = 120.0) -> bool:
    """Poll the Tor log until it reports a fully bootstrapped circuit. A cold first start (building
    the consensus from scratch) can take well over a minute, so the timeout is generous."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _proc is not None and _proc.poll() is not None:
            _log(f"tor exited early (code {_proc.returncode}) during bootstrap")
            return False
        if _log_bootstrapped():
            return True
        time.sleep(0.5)
    return False


def start(timeout: float = 120.0) -> bool:
    """Ensure a binary, launch our Tor instance, and wait for bootstrap. No-op (False) if managed
    Tor is disabled, no binary is available, or launch fails — the app then uses the manual proxy."""
    global _proc, _socks_port, _bootstrapped
    if not managed_enabled():
        return False
    if is_running():
        return _bootstrapped
    binary = ensure_binary()
    if not binary:
        _log("start: no tor binary available (manual proxy will be used)")
        return False
    try:
        DATA_SUBDIR.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        TORRC.write_text(build_torrc(port), encoding="utf-8")
        try:
            if LOG.exists():
                LOG.unlink()   # start fresh so wait_bootstrap reads THIS run's log
        except OSError:
            pass               # e.g. another (orphan) tor holds it open — don't abort the launch
        flags = subprocess.CREATE_NO_WINDOW if _WINDOWS else 0  # type: ignore[attr-defined]
        _proc = subprocess.Popen([binary, "-f", str(TORRC)],  # noqa: S603
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=flags)
        _socks_port = port
        _log(f"start: launched tor pid={_proc.pid} on SocksPort {port}")
    except Exception as exc:  # noqa: BLE001
        _log(f"start: launch failed: {exc!r}")
        _proc = None
        _socks_port = None
        return False
    _bootstrapped = wait_bootstrap(timeout)
    _log(f"start: bootstrapped={_bootstrapped}")
    return _bootstrapped


def is_running() -> bool:
    return _proc is not None and _proc.poll() is None


def is_ready() -> bool:
    """Whether our Tor has finished bootstrapping. Dynamic (re-reads the log + caches once seen), so
    a slow cold start that outran the initial wait still flips to ready instead of latching False."""
    global _bootstrapped
    if not _bootstrapped and is_running() and _log_bootstrapped():
        _bootstrapped = True
    return _bootstrapped and is_running()


def active_proxy() -> tuple[str, int] | None:
    """The managed SOCKS proxy (host, port) whenever OUR Tor process is up. We route through it as
    soon as it's running (even during the last % of bootstrap) so the app never falls back to a
    possibly-dead manual proxy (e.g. a closed Tor Browser's 9150) — which was the WinError-10061
    cause. Returns None only when managed Tor isn't running (headless/StartOS use the manual proxy)."""
    if is_running() and _socks_port:
        return ("127.0.0.1", _socks_port)
    return None


def stop() -> None:
    global _proc, _socks_port, _bootstrapped
    if _proc is not None:
        try:
            _proc.terminate()
            try:
                _proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _proc.kill()
        except Exception as exc:  # noqa: BLE001
            _log(f"stop: {exc!r}")
    _proc = None
    _socks_port = None
    _bootstrapped = False


# --- update / CVE path -------------------------------------------------------
def check_update() -> dict:
    """Compare the installed version against the latest published one."""
    cur = installed_version()
    latest = latest_version()
    available = bool(latest) and _version_newer(latest, cur)
    return {"installed": cur, "latest": latest, "update_available": available}


def update() -> dict:
    """Download+verify the latest build. If Tor is running it's swapped in on the next launch."""
    latest = latest_version()
    if not latest:
        return {"ok": False, "error": "could not determine the latest version"}
    if not _version_newer(latest, installed_version()):
        return {"ok": True, "updated": False, "latest": latest, "update_available": False}
    if download(latest):
        return {"ok": True, "updated": True, "version": latest}
    return {"ok": False, "error": "download/verify failed (see data/tor/service.log)"}


def status() -> dict:
    return {
        "enabled": managed_enabled(),
        "running": is_running(),
        "bootstrapped": is_ready(),
        "socks_port": _socks_port,
        "version": installed_version(),
    }
