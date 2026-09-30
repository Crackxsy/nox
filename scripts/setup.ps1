<#
.SYNOPSIS
    Sets a fresh checkout up: dependencies, both web UIs, and a machine check.

.DESCRIPTION
    Everything `docs/GETTING_STARTED.md` step 1 and 2 describe, in one command, because five
    commands across three folders is where people give up - and forgetting the UI builds is the
    single most common way to end up with a pet window that renders nothing.

    It stops at the first failure and says which step failed. It does not start Nox and it does not
    run the onboarding wizard: both ask questions, and a setup script that starts asking is a setup
    script nobody can run unattended.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#>
[CmdletBinding()]
param(
    # Skip the optional voice extra, which pulls a large speech stack.
    [switch]$NoVoice
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot

function Step([string]$what, [scriptblock]$action) {
    Write-Host ""
    Write-Host "== $what" -ForegroundColor Cyan
    & $action
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   failed: $what" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Nox setup in $repo"

# python.org Python, not the Store build: the Store one redirects %APPDATA% writes into a package
# cache, so the config and token end up somewhere other than where every path says they are.
$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) {
    Write-Host "python is not on PATH. Install Python 3.13 from python.org." -ForegroundColor Red
    exit 1
}
if ($python.Source -like '*WindowsApps*') {
    Write-Host "Warning: this is Microsoft Store Python. Nox will run, but %APPDATA% writes are" -ForegroundColor Yellow
    Write-Host "redirected into the package cache. python.org Python is the supported one." -ForegroundColor Yellow
}

Step "uv" { python -m pip install --quiet --upgrade uv }

$extras = @('--extra', 'dev', '--extra', 'shell')
if (-not $NoVoice) { $extras += @('--extra', 'voice') }
Step "dependencies" { python -m uv sync @extras }

foreach ($ui in @('pet', 'dashboard')) {
    Step "ui/$ui" {
        Push-Location (Join-Path $repo "ui\$ui")
        try {
            npm ci
            if ($LASTEXITCODE -eq 0) { npm run build }
        } finally { Pop-Location }
    }
}

Step "doctor" { & (Join-Path $repo '.venv\Scripts\nox.exe') doctor }

Write-Host ""
Write-Host "Ready. Two more commands, both of yours to run:" -ForegroundColor Green
Write-Host "  .venv\Scripts\nox onboard      set it up (asks questions)"
Write-Host "  .venv\Scripts\nox supervisor   start it"
