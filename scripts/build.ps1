<#
.SYNOPSIS
    Build OrcMesh for Windows as a standalone executable.
.PARAMETER SkipTests
    Skip the test suite (not recommended for release builds).
.PARAMETER NoUpx
    Disable UPX compression (faster build, larger exe).
#>
param(
    [switch]$SkipTests,
    [switch]$NoUpx
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Error "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
    exit 1
}

# ── Map vendor assets — Leaflet/MarkerCluster plus qwebchannel.js ───────────
# Checked as a set: gating on leaflet.js alone meant a partially populated
# vendor/ (Leaflet present, qwebchannel.js missing) was never repaired, so the
# build shipped a map whose JS bridge could never connect — the basemap drew
# fine but no node pin ever appeared.
$vendorDir = "src\meshchat\ui\map\web\vendor"
$requiredVendorAssets = @(
    "$vendorDir\leaflet\leaflet.js",
    "$vendorDir\leaflet\leaflet.css",
    "$vendorDir\markercluster\leaflet.markercluster.js",
    "$vendorDir\qwebchannel.js"
)
$missingVendorAssets = @($requiredVendorAssets | Where-Object { -not (Test-Path $_) })
if ($missingVendorAssets.Count -gt 0) {
    Write-Host "==> Fetching missing map vendor assets..." -ForegroundColor Cyan
    $missingVendorAssets | ForEach-Object { Write-Host "    missing: $_" }
    & ".\.venv\Scripts\python.exe" scripts\fetch_vendors.py
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Map vendor assets could not be prepared — the map would ship without a working bridge"
        exit 1
    }
}

# ── Run test suite ──────────────────────────────────────────────────────────
if (-not $SkipTests) {
    Write-Host "==> Running tests..." -ForegroundColor Cyan
    & ".\.venv\Scripts\python.exe" -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Tests failed — aborting build"
        exit 1
    }
}

# ── Clean previous build ────────────────────────────────────────────────────
Write-Host "==> Cleaning previous build..." -ForegroundColor Cyan
if (Test-Path "build") { Remove-Item -Recurse -Force build }
if (Test-Path "dist")  { Remove-Item -Recurse -Force dist  }

# ── PyInstaller ─────────────────────────────────────────────────────────────
Write-Host "==> Running PyInstaller..." -ForegroundColor Cyan
$specArgs = @("packaging\orcmesh.spec", "--clean", "--noconfirm")
if ($NoUpx) { $specArgs += "--noupx" }

& ".\.venv\Scripts\pyinstaller.exe" @specArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed"
    exit 1
}

# ── Post-build: copy QtWebEngineProcess.exe if PyInstaller missed it ────────
Write-Host "==> Post-build checks..." -ForegroundColor Cyan
$distDir = "dist\OrcMesh"
$pyside6Src = & ".\.venv\Scripts\python.exe" -c "import PySide6, os; print(os.path.dirname(PySide6.__file__))"

# QtWebEngineProcess.exe — must be in the root of the dist folder on Windows
$webEngineProc = Join-Path $pyside6Src "QtWebEngineProcess.exe"
if ((Test-Path $webEngineProc) -and -not (Test-Path "$distDir\QtWebEngineProcess.exe")) {
    Write-Host "    Copying QtWebEngineProcess.exe..."
    Copy-Item $webEngineProc "$distDir\"
}

# icudtl.dat — required for QtWebEngine
$icuDat = Join-Path $pyside6Src "resources\icudtl.dat"
if ((Test-Path $icuDat) -and -not (Test-Path "$distDir\icudtl.dat")) {
    Write-Host "    Copying icudtl.dat..."
    Copy-Item $icuDat "$distDir\"
}

# ── Verify ──────────────────────────────────────────────────────────────────
if (Test-Path "$distDir\OrcMesh.exe") {
    $size = (Get-Item "$distDir\OrcMesh.exe").Length / 1MB
    Write-Host ""
    Write-Host ("==> Build successful: $distDir\OrcMesh.exe ({0:F1} MB)" -f $size) -ForegroundColor Green
    Write-Host "    Full dist directory: $((Get-Item $distDir).FullName)"
} else {
    Write-Error "Build failed — OrcMesh.exe not found in dist\"
    exit 1
}
