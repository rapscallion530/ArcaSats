# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rapscallion
"""Desktop launcher — run ArcaSats in a native OS window (pywebview / Windows WebView2) instead
of a browser tab, with no console. Closing the window stops the server and exits the process, so
there's no Ctrl+C in a terminal.

This is the Windows double-click path (run.bat → run.ps1 launches `pythonw desktop.py`). The
server / StartOS deployment is unaffected: it still runs `uvicorn app.main:app` headless and never
imports pywebview (which isn't in the core requirements or the Docker image).

The server runs in a daemon thread (uvicorn skips signal handlers off the main thread); the GUI
owns the main thread, as its toolkit requires.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

HOST = "127.0.0.1"
TITLE = "ArcaSats"


def _ensure_streams() -> None:
    """Under pythonw.exe (no console) sys.stdout/sys.stderr are None, which crashes uvicorn's
    logging setup (its formatter calls sys.stdout.isatty()) and any stray print(). Point the
    missing streams at the desktop log file so nothing blows up before the window appears."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        from app.config import DATA_DIR
        stream = open(os.path.join(DATA_DIR, "desktop.log"), "a", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001
        stream = open(os.devnull, "w")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _free_port(preferred: int = 8000) -> int:
    """Use the preferred port if free, else an OS-assigned ephemeral one (the native window points
    at whatever we pick, so the exact number doesn't matter and we avoid 'port in use')."""
    for candidate in (preferred, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((HOST, candidate))
                return s.getsockname()[1]
        except OSError:
            continue
    return preferred


def _start_server(port: int):
    """Start uvicorn in a background daemon thread; return (server, thread) once it's accepting."""
    import uvicorn

    from app.main import app

    # log_config=None: don't install uvicorn's console logging (its colored formatter probes
    # sys.stdout.isatty(), which is fatal under pythonw); we run windowed, not in a terminal.
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning", log_config=None)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="arcasats-uvicorn", daemon=True)
    thread.start()
    for _ in range(400):  # up to ~20s for startup
        if server.started:
            break
        time.sleep(0.05)
    return server, thread


def _open_native_window(url: str) -> None:
    """Blocks until the window is closed."""
    import webview  # noqa: PLC0415  (optional desktop-only dep)

    webview.create_window(TITLE, url, width=1200, height=820, min_size=(900, 600))
    webview.start()


def main(run_window=_open_native_window, min_session_s: float = 3.0, on_started=None) -> int:
    """Launch the server + native window. `run_window` is injected for tests; normally it blocks
    until the user closes the window, after which we stop the server and exit.

    Robustness: some systems can't display a WebView2 window (missing runtime, no desktop session,
    etc.), in which case `webview.start()` returns almost immediately — or raises. Either way we
    must NOT silently exit (that's the 'nothing happens' bug). If the window errors or its session
    was implausibly short, we degrade to opening a browser tab against the still-running server.
    Everything is logged to data/desktop.log so a windowed (console-less) launch is diagnosable.
    """
    _ensure_streams()
    port = _free_port(int(os.environ.get("BTT_PORT", "8000")))
    server, thread = _start_server(port)
    url = f"http://{HOST}:{port}"
    _log(f"server started at {url} (started={server.started}); opening native window")
    if on_started:
        try:
            on_started(url)
        except Exception:  # noqa: BLE001
            pass

    error = None
    began = time.monotonic()
    try:
        run_window(url)
    except Exception as exc:  # noqa: BLE001
        error = exc
    elapsed = time.monotonic() - began

    if error is None and elapsed >= min_session_s:
        _log(f"window closed after {elapsed:.1f}s; shutting down")
        server.should_exit = True
        thread.join(timeout=5)
        return 0

    # Window unavailable (raised, or returned too fast to have really shown). Don't leave a blank
    # screen: open a browser tab against the running server and keep serving.
    _log(f"native window unavailable (elapsed={elapsed:.1f}s, error={error!r}); "
         f"opening a browser tab at {url} and keeping the server running")
    import webbrowser
    webbrowser.open(url)
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    server.should_exit = True
    return 0


def _log(message: str) -> None:
    try:
        from app.config import DATA_DIR
        with open(os.path.join(DATA_DIR, "desktop.log"), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:  # noqa: BLE001 — logging must never crash the launcher
        pass


def _lock_path() -> str:
    from app.config import DATA_DIR
    return os.path.join(DATA_DIR, "desktop.lock")


def existing_instance_url() -> str | None:
    """If another ArcaSats desktop instance is already serving, return its URL (else None). Lets a
    second launch reuse it instead of starting a colliding second server + second Tor (which fight
    over the port and Tor's data-directory lock)."""
    try:
        with open(_lock_path(), encoding="utf-8") as fh:
            url = fh.read().strip()
    except OSError:
        return None
    if not url:
        return None
    try:
        import urllib.request
        with urllib.request.urlopen(url + "/health", timeout=2):  # noqa: S310 (loopback)
            return url
    except Exception:  # noqa: BLE001 — unreachable => stale lock, ignore
        return None


def _write_lock(url: str) -> None:
    try:
        with open(_lock_path(), "w", encoding="utf-8") as fh:
            fh.write(url)
    except OSError:
        pass


def _clear_lock() -> None:
    try:
        os.remove(_lock_path())
    except OSError:
        pass


def _managed_tor_startup() -> None:
    """Best-effort, in the background: launch ArcaSats's OWN Tor (so a .onion node works with no
    second app) and note if a newer Tor is available (CVE hygiene). Desktop launch only."""
    try:
        from app.services import tor_service
        tor_service.start()
        upd = tor_service.check_update()
        if upd.get("update_available"):
            tor_service._log(f"update available: {upd.get('installed')} -> {upd.get('latest')}")
    except Exception as exc:  # noqa: BLE001
        _log(f"managed tor startup failed: {exc!r}")


if __name__ == "__main__":
    _ensure_streams()
    _log("launcher starting")
    # Single instance: if one's already running, just show a window on it and exit — don't start a
    # second server/Tor that would collide on the port and Tor's data-dir lock.
    _existing = existing_instance_url()
    if _existing:
        _log(f"another instance already running at {_existing}; opening a window to it")
        try:
            _open_native_window(_existing)
        except Exception as exc:  # noqa: BLE001
            _log(f"could not open window to existing instance ({exc!r}); using browser")
            import webbrowser
            webbrowser.open(_existing)
        raise SystemExit(0)
    # Turn on the bundled/managed Tor for the desktop app (headless/StartOS leaves this off and
    # uses the system Tor). Launch it in the background so the window appears without waiting on
    # Tor's bootstrap; the app falls back to any manually-configured proxy until it's ready.
    os.environ.setdefault("BTT_MANAGED_TOR", "1")
    threading.Thread(target=_managed_tor_startup, name="arcasats-tor", daemon=True).start()
    try:
        rc = main(on_started=_write_lock)
    except Exception as exc:  # noqa: BLE001 — no console under pythonw; leave a trace before dying
        _log(f"FATAL: {exc!r}")
        raise
    finally:
        try:
            from app.services import tor_service
            tor_service.stop()
        except Exception:  # noqa: BLE001
            pass
        _clear_lock()
    raise SystemExit(rc)
