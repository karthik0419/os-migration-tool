<#
.SYNOPSIS
  One-click setup for the OpenSearch Migration Tool.
.DESCRIPTION
  Creates the Python venv, installs server deps, installs + builds the UI,
  starts the server, and opens the browser. Re-runnable — skips steps
  that are already done.
.PARAMETER Port
  Server port (default 8020).
.PARAMETER NoStart
  Set up only, don't start the server or open the browser.
.EXAMPLE
  .\setup.ps1
  .\setup.ps1 -Port 9090
  .\setup.ps1 -NoStart
#>
param(
  [int]$Port = 8020,
  [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Server = Join-Path $Root "server"
$UI     = Join-Path $Root "ui"
$VenvPy = Join-Path $Server ".venv\Scripts\python.exe"

function Write-Step($msg) { Write-Host "`n[setup] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  OK: $msg" -ForegroundColor Green }
function Write-Skip($msg) { Write-Host "  skip: $msg (already done)" -ForegroundColor DarkGray }

# --- 1. Python venv + server deps ---
Write-Step "Setting up Python environment"
if (Test-Path $VenvPy) {
  Write-Skip "venv exists"
} else {
  Write-Host "  Creating venv..."
  Push-Location $Server
  python -m venv .venv
  Pop-Location
  Write-Ok "venv created"
}

Write-Step "Installing server dependencies"
$installed = & $VenvPy -m pip list 2>$null | Select-String "fastapi"
if ($installed) {
  Write-Skip "server deps installed"
} else {
  & $VenvPy -m pip install -r (Join-Path $Server "requirements.txt") --quiet
  Write-Ok "server deps installed"
}

# --- 2. UI build ---
Write-Step "Setting up UI"
$NodeModules = Join-Path $UI "node_modules"
$DistIndex   = Join-Path $UI "dist\index.html"
if (Test-Path $NodeModules) {
  Write-Skip "node_modules exists"
} else {
  Write-Host "  Installing npm packages (this can take a minute)..."
  Push-Location $UI
  # Reuse lockfile from a sibling frontend/ if npm cache is flaky (corporate TLS)
  npm install --prefer-offline --no-audit --no-fund 2>&1 | Out-Null
  Pop-Location
  Write-Ok "npm packages installed"
}

if (Test-Path $DistIndex) {
  Write-Skip "UI already built"
} else {
  Write-Host "  Building UI..."
  Push-Location $UI
  npm run build 2>&1 | Out-Null
  Pop-Location
  if (Test-Path $DistIndex) {
    Write-Ok "UI built"
  } else {
    Write-Host "  UI build failed — you can still use 'npm run dev' for hot-reload" -ForegroundColor Yellow
  }
}

# --- 3. Start server + open browser ---
if ($NoStart) {
  Write-Step "Setup complete (server not started — run with: server\.venv\Scripts\python.exe server\app.py)"
  return
}

Write-Step "Starting server on port $Port"
$env:OSMT_PORT = $Port
# Check if something is already listening on the port
$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
  Write-Skip "server already running on :$Port"
} else {
  Start-Process -FilePath $VenvPy -ArgumentList "app.py" -WorkingDirectory $Server -WindowStyle Minimized
  Start-Sleep 3
  $listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  if ($listening) {
    Write-Ok "server started on :$Port (PID $($listening.OwningProcess))"
  } else {
    Write-Host "  Server may still be starting — check http://127.0.0.1:$Port" -ForegroundColor Yellow
  }
}

Write-Step "Opening browser"
Start-Process "http://127.0.0.1:$Port"
Write-Ok "browser opened"
Write-Host "`n[setup] Done! The tool is at http://127.0.0.1:$Port`n" -ForegroundColor Cyan
