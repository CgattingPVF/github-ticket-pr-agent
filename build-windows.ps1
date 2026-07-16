[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildVenv = Join-Path $ProjectRoot ".build-venv"
$Python = Join-Path $BuildVenv "Scripts\python.exe"

if ($env:OS -ne "Windows_NT") {
    throw "This script must run on Windows; PyInstaller cannot cross-compile a Windows executable."
}

Push-Location $ProjectRoot
try {
    if (-not (Test-Path $Python)) {
        py -3 -m venv $BuildVenv
    }

    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r requirements-build.txt

    if (-not $SkipTests) {
        & $Python -m pytest -q
        if ($LASTEXITCODE -ne 0) {
            throw "Tests failed; executable was not built."
        }
    }

    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name ticket-pr-agent `
        --add-data "templates;templates" `
        --add-data "static;static" `
        launcher.py

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed."
    }

    Write-Host "Built: $ProjectRoot\dist\ticket-pr-agent.exe"
}
finally {
    Pop-Location
}
