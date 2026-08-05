<#
.SYNOPSIS
    Run the MeshChat test suite.
.PARAMETER Fast
    Skip slow integration tests (marked with @pytest.mark.slow).
.PARAMETER Args
    Extra pytest arguments (e.g. tests/test_controller.py -v).
#>
param(
    [switch]$Fast,
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$PytestArgs
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Error "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
    exit 1
}

$extra = @()
if ($Fast) { $extra += "-m"; $extra += "not slow" }
if ($PytestArgs) { $extra += $PytestArgs }

Write-Host "==> Running tests..." -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -m pytest -q @extra
