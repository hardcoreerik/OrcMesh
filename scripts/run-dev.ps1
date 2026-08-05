<#
.SYNOPSIS
    Run MeshChat in development mode from the local venv.
.PARAMETER Debug
    Enable verbose debug logging.
.PARAMETER Args
    Any additional arguments passed through to the app (e.g. --device, --ble).
#>
param(
    [switch]$Debug,
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$AppArgs
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Error "Virtual environment not found. Run .\scripts\bootstrap.ps1 first."
    exit 1
}

$extraArgs = @()
if ($Debug) { $extraArgs += "--debug" }
if ($AppArgs) { $extraArgs += $AppArgs }

Write-Host "==> Launching MeshChat (dev)..." -ForegroundColor Cyan
& ".\.venv\Scripts\python.exe" -m meshchat @extraArgs
