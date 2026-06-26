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
import threading
import time

HOST = "127.0.0.1"
TITLE = "ArcaSats"


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

    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
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


def main(run_window=_open_native_window) -> int:
    """Launch the server + native window. `run_window` is injected for tests; it must block until
    the user closes the window, after which we stop the server and return."""
    port = _free_port(int(os.environ.get("BTT_PORT", "8000")))
    server, thread = _start_server(port)
    url = f"http://{HOST}:{port}"
    try:
        run_window(url)
    except Exception as exc:  # noqa: BLE001 — windowed mode has no console; degrade to a browser tab
        _log(f"native window failed ({exc!r}); opening a browser tab instead")
        import webbrowser
        webbrowser.open(url)
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
    finally:
        server.should_exit = True
        thread.join(timeout=5)
    return 0


def _log(message: str) -> None:
    try:
        from app.config import DATA_DIR
        with open(os.path.join(DATA_DIR, "desktop.log"), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:  # noqa: BLE001 — logging must never crash the launcher
        pass


if __name__ == "__main__":
    raise SystemExit(main())
