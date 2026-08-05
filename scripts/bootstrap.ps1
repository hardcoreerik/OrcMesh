<#
.SYNOPSIS
    Bootstrap the MeshChat development environment.
.DESCRIPTION
    Creates a virtual environment, installs the package in editable mode with
    all dev dependencies.  Run once after cloning, or again to upgrade deps.
#>
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "==> MeshChat bootstrap" -ForegroundColor Cyan

# ── Python version check ────────────────────────────────────────────────────
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $py) {
    Write-Error "Python 3.12+ not found on PATH. Install from https://python.org"
    exit 1
}

$ver = & $py.Source --version 2>&1
Write-Host "    Python: $ver"

# ── Virtual environment ─────────────────────────────────────────────────────
if (-not (Test-Path ".venv")) {
    Write-Host "==> Creating virtual environment..." -ForegroundColor Cyan
    & $py.Source -m venv .venv
}

$pip = ".\.venv\Scripts\pip.exe"

# Upgrade pip quietly
Write-Host "==> Upgrading pip..." -ForegroundColor Cyan
& $pip install --quiet --upgrade pip

# ── Install project + dev extras ────────────────────────────────────────────
Write-Host "==> Installing meshchat[dev]..." -ForegroundColor Cyan
& $pip install --quiet -e ".[dev]"

# ── Verify key imports ──────────────────────────────────────────────────────
Write-Host "==> Verifying key imports..." -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -c @"
import meshtastic, PySide6, pyqtgraph, platformdirs, pubsub
print('    meshtastic', getattr(meshtastic,'__version__','?'))
print('    PySide6', PySide6.__version__)
print('    pyqtgraph', pyqtgraph.__version__)
"@

Write-Host ""
Write-Host "==> Bootstrap complete." -ForegroundColor Green
Write-Host "    Run the app:   .\scripts\run-dev.ps1"
Write-Host "    Run tests:     .\scripts\test.ps1"
Write-Host "    Build exe:     .\scripts\build.ps1"
