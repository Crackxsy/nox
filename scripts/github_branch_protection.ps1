<#
.SYNOPSIS
    Applies (or previews) the branch-protection rules for Nox's default branch via `gh api`.

.DESCRIPTION
    Idempotent: the script reads the branch's current protection, compares it with the desired
    state, and only sends a request when something actually differs. Run it as often as you like.

    The required status checks default to the exact job names in `.github/workflows/ci.yml`.
    If a job is renamed there, rename it here too (or pass -RequiredChecks) - GitHub matches
    status checks by their display name, and a name that never reports will block every merge.

    NOTE: on GitHub Free, branch protection and rulesets are only available for PUBLIC
    repositories. Running this against a private repo on a Free plan fails with HTTP 403.

.PARAMETER Repo
    owner/name of the repository. Default: Crackxsy/nox.

.PARAMETER Branch
    Branch to protect. Default: main.

.PARAMETER RequiredApprovals
    Number of approving reviews a pull request needs. Default 0, which still forces every change
    through a pull request with green CI but lets a solo maintainer merge without a second person.
    Set to 1 as soon as there is someone else who can review.

.PARAMETER RequireCodeOwnerReview
    Also require a review from a CODEOWNERS owner. Do not use this with a solo maintainer: you
    cannot approve your own pull request, so it would block every merge.

.PARAMETER RequiredChecks
    Override the list of required status-check names.

.PARAMETER NoStrict
    Do not require branches to be up to date with the base branch before merging.

.PARAMETER DryRun
    Print what would be sent and exit without changing anything.

.EXAMPLE
    pwsh -File scripts/github_branch_protection.ps1 -DryRun

.EXAMPLE
    pwsh -File scripts/github_branch_protection.ps1 -Repo Crackxsy/nox -RequiredApprovals 1 -RequireCodeOwnerReview
#>
[CmdletBinding()]
param(
    [string]$Repo = 'Crackxsy/nox',
    [string]$Branch = 'main',
    [ValidateRange(0, 6)]
    [int]$RequiredApprovals = 0,
    [switch]$RequireCodeOwnerReview,
    [string[]]$RequiredChecks = @(
        'Python (ruff, mypy, unit + integration tests)',
        'UI (pet)',
        'UI (dashboard)',
        'Guard - no input-synthesis / process-memory APIs (Security Model §10)',
        'Release hygiene (third-party license audit, secrets scan)',
        'E2E (Playwright live-core smoke)',
        'Docs (relative links)'
    ),
    [switch]$NoStrict,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSDefaultParameterValues['*:Encoding'] = 'utf8'

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "    $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "    $Message" -ForegroundColor Yellow }

# ---------------------------------------------------------------------------- preconditions ----

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) not found. Install it from https://cli.github.com and run 'gh auth login'."
}

& gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    throw "gh is not authenticated. Run 'gh auth login' first."
}

Write-Step "Repository $Repo, branch '$Branch'"

$repoJson = & gh api "repos/$Repo" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Cannot read repos/$Repo - does it exist and does your token have access?"
}
$repoInfo = $repoJson | ConvertFrom-Json
if ($repoInfo.private) {
    Write-Warn 'This repository is PRIVATE. Branch protection needs GitHub Team/Enterprise for private repos; on the Free plan this will fail with HTTP 403.'
}
if ($repoInfo.default_branch -ne $Branch) {
    Write-Warn "The repository default branch is '$($repoInfo.default_branch)', not '$Branch'."
}

# --------------------------------------------------------------------------- desired state -----

$reviews = [ordered]@{
    dismiss_stale_reviews           = $true
    require_code_owner_reviews      = [bool]$RequireCodeOwnerReview
    required_approving_review_count = $RequiredApprovals
    require_last_push_approval      = $false
}

$desired = [ordered]@{
    required_status_checks          = [ordered]@{
        strict   = (-not $NoStrict)
        contexts = @($RequiredChecks)
    }
    enforce_admins                  = $true
    required_pull_request_reviews   = $reviews
    restrictions                    = $null
    required_linear_history         = $true
    allow_force_pushes              = $false
    allow_deletions                 = $false
    block_creations                 = $false
    required_conversation_resolution = $true
    lock_branch                     = $false
    allow_fork_syncing              = $false
}

$payload = $desired | ConvertTo-Json -Depth 8

# ------------------------------------------------------------------------------- comparison ----

function Get-CurrentProtection {
    $json = & gh api "repos/$Repo/branches/$Branch/protection" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return $json | ConvertFrom-Json
}

function Test-ProtectionMatches {
    param($Current)
    if ($null -eq $Current) { return $false }
    try {
        $checks = $Current.required_status_checks
        if ($null -eq $checks) { return $false }
        if ([bool]$checks.strict -ne (-not $NoStrict)) { return $false }
        $have = @($checks.contexts | Sort-Object)
        $want = @($RequiredChecks | Sort-Object)
        if ($have.Count -ne $want.Count) { return $false }
        for ($i = 0; $i -lt $want.Count; $i++) {
            if ($have[$i] -cne $want[$i]) { return $false }
        }

        if (-not [bool]$Current.enforce_admins.enabled) { return $false }
        if (-not [bool]$Current.required_linear_history.enabled) { return $false }
        if ([bool]$Current.allow_force_pushes.enabled) { return $false }
        if ([bool]$Current.allow_deletions.enabled) { return $false }
        if (-not [bool]$Current.required_conversation_resolution.enabled) { return $false }

        $rv = $Current.required_pull_request_reviews
        if ($null -eq $rv) { return $false }
        if (-not [bool]$rv.dismiss_stale_reviews) { return $false }
        if ([bool]$rv.require_code_owner_reviews -ne [bool]$RequireCodeOwnerReview) { return $false }
        if ([int]$rv.required_approving_review_count -ne $RequiredApprovals) { return $false }

        return $true
    }
    catch {
        return $false
    }
}

Write-Step 'Reading current protection'
$current = Get-CurrentProtection
if ($null -eq $current) {
    Write-Warn "No branch protection configured yet (or not readable)."
}

Write-Step 'Desired protection'
Write-Host $payload

if (Test-ProtectionMatches -Current $current) {
    Write-Ok 'Already up to date - nothing to do.'
    exit 0
}

if ($DryRun) {
    Write-Warn 'DRY RUN: the payload above would be PUT to'
    Write-Warn "         repos/$Repo/branches/$Branch/protection"
    exit 0
}

# ---------------------------------------------------------------------------------- apply ------

Write-Step 'Applying protection'
$tmp = New-TemporaryFile
try {
    # -Encoding utf8 (no BOM in PowerShell 7) so the '§' in a job name survives the round trip.
    Set-Content -Path $tmp -Value $payload -Encoding utf8 -NoNewline
    & gh api --method PUT "repos/$Repo/branches/$Branch/protection" `
        -H 'Accept: application/vnd.github+json' `
        --input $tmp | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "gh api PUT failed with exit code $LASTEXITCODE (403 usually means: private repository on a Free plan)."
    }
}
finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}

Write-Step 'Verifying'
$after = Get-CurrentProtection
if (Test-ProtectionMatches -Current $after) {
    Write-Ok "Branch protection applied to $Repo@$Branch."
    Write-Ok "Required checks: $($RequiredChecks -join '; ')"
    Write-Ok "Required approvals: $RequiredApprovals; linear history: on; force pushes: off; deletions: off."
    exit 0
}

Write-Warn 'The PUT succeeded but the read-back does not match the desired state. Compare manually:'
Write-Warn "  gh api repos/$Repo/branches/$Branch/protection"
exit 1
