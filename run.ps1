# Launch bitcoin-tax-tracker locally on Windows.
# First run creates a virtualenv and installs dependencies; later runs just start it.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# Optional local-only settings (gitignored), e.g. your electrs host + bind address.
if (Test-Path "env.local.ps1") { . .\env.local.ps1 }

# Bind address: defaults to localhost; set BTT_BIND_HOST (e.g. a Tailscale IP) to
# expose to your tailnet only. 0.0.0.0 = all interfaces (also the LAN).
$bindHost = if ($env:BTT_BIND_HOST) { $env:BTT_BIND_HOST } else { "127.0.0.1" }

if (-not (Test-Path ".venv")) {
    Write-Host "First run: creating virtualenv and installing dependencies..."
    py -3 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
}

$displayHost = if ($bindHost -eq "0.0.0.0") { "127.0.0.1" } else { $bindHost }
Write-Host "Starting bitcoin-tax-tracker at http://${displayHost}:8000  (Ctrl+C to stop)"
Start-Process "http://${displayHost}:8000"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host $bindHost --port 8000
