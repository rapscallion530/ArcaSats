@echo off
REM Double-click to launch ArcaSats. It opens in its own window (native WebView2); close the
REM window to quit. First run sets up a local environment (~30s). If a native window isn't
REM available, it falls back to opening http://127.0.0.1:8000 in your browser.
powershell -ExecutionPolicy Bypass -File "%~dp0run.ps1"
REM Keep this console open only if launch failed (the windowed path exits cleanly).
if errorlevel 1 pause
