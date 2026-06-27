# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The ArcaSats Authors
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


def main(run_window=_open_native_window, min_session_s: float = 3.0) -> int:
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


if __name__ == "__main__":
    _log("launcher starting")
    try:
        rc = main()
    except Exception as exc:  # noqa: BLE001 — no console under pythonw; leave a trace before dying
        _log(f"FATAL: {exc!r}")
        raise
    raise SystemExit(rc)
