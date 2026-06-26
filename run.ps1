# Launch ArcaSats locally on Windows.
# First run creates a virtualenv and installs dependencies; later runs just start it.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Optional local-only settings (gitignored), e.g. your electrs host + bind address.
if (Test-Path "env.local.ps1") { . .\env.local.ps1 }

# Bind address: defaults to localhost; set BTT_BIND_HOST (e.g. a Tailscale IP) to
# expose to your tailnet only. 0.0.0.0 = all interfaces (also the LAN).
$bindHost = if ($env:BTT_BIND_HOST) { $env:BTT_BIND_HOST } else { "127.0.0.1" }

if (-not (Test-Path ".venv")) {
    # Find a Python interpreter: prefer the `py` launcher, else `python` on PATH.
    $pyCmd = $null; $pyArgs = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $pyCmd = "py"; $pyArgs = @("-3") }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyCmd = "python" }
    if (-not $pyCmd) {
        Write-Host ""
        Write-Host "Python was not found." -ForegroundColor Yellow
        Write-Host "Install Python 3.12 or newer from https://www.python.org/downloads/" -ForegroundColor Yellow
        Write-Host "and tick 'Add python.exe to PATH' during setup, then run this again." -ForegroundColor Yellow
        exit 1
    }
    Write-Host "First run: creating a virtual environment and installing dependencies (one-time)..."
    & $pyCmd @pyArgs -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
}

$displayHost = if ($bindHost -eq "0.0.0.0") { "127.0.0.1" } else { $bindHost }
Write-Host "Starting ArcaSats at http://${displayHost}:8000  (Ctrl+C to stop)"
Start-Process "http://${displayHost}:8000"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host $bindHost --port 8000
