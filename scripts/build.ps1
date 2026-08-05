<#
.SYNOPSIS
    Build MeshChat for Windows as a standalone executable.
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

# ── Vendor Leaflet assets if not already present ────────────────────────────
$vendorDir = "src\meshchat\ui\map\web\vendor"
if (-not (Test-Path "$vendorDir\leaflet\leaflet.js")) {
    Write-Host "==> Downloading Leaflet vendor assets..." -ForegroundColor Cyan
    & ".\.venv\Scripts\python.exe" scripts\fetch_vendors.py
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
$specArgs = @("packaging\meshchat.spec", "--clean", "--noconfirm")
if ($NoUpx) { $specArgs += "--noupx" }

& ".\.venv\Scripts\pyinstaller.exe" @specArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed"
    exit 1
}

# ── Post-build: copy QtWebEngineProcess.exe if PyInstaller missed it ────────
Write-Host "==> Post-build checks..." -ForegroundColor Cyan
$distDir = "dist\MeshChat"
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
if (Test-Path "$distDir\MeshChat.exe") {
    $size = (Get-Item "$distDir\MeshChat.exe").Length / 1MB
    Write-Host ""
    Write-Host ("==> Build successful: $distDir\MeshChat.exe ({0:F1} MB)" -f $size) -ForegroundColor Green
    Write-Host "    Full dist directory: $((Get-Item $distDir).FullName)"
} else {
    Write-Error "Build failed — MeshChat.exe not found in dist\"
    exit 1
}
