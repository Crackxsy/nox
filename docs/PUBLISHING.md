# Publishing Nox as a public repository

This is the runbook for taking Nox from a private repository to a public one. It is written to be
executed top to bottom, once, by the maintainer.

## The decision

Every commit in the current history is authored with a real name and a personal e-mail address.
Git history cannot be changed after publication without rewriting it, and a rewrite of an already
public history is worse than useless — forks and caches keep the old objects.

There were two honest options:

- **Option A — publish this repository as it is.** Everything keeps working (issues, stars, the
  existing history, `git blame`, the CI runs), but the author name and personal e-mail address stay
  visible in all commits, forever, to everyone. **Not chosen.**
- **Option B — publish a fresh repository built from a single squashed snapshot**, authored with a
  GitHub `users.noreply` address, and delete the old repository afterwards. The cost is the loss of
  the granular history and of `git blame` before the snapshot commit; the benefit is that no
  personal e-mail address is ever published. **Chosen (2026-09-16).**

The old repository is kept, renamed, as a private archive only until the new repository's CI is
green — then it is deleted. That deletion is deliberate and irreversible.

## Before you start

You need the GitHub CLI, authenticated as the account that owns the repository:

```powershell
gh auth status
gh auth refresh -h github.com -s delete_repo   # the token needs delete_repo for the last step
```

Look up your GitHub numeric user ID once — it is part of the noreply address:

```powershell
gh api user --jq '"\(.id)+\(.login)@users.noreply.github.com"'
# -> e.g. 12345678+Crackxsy@users.noreply.github.com
```

Make sure "Keep my email addresses private" and "Block command line pushes that expose my email"
are enabled under <https://github.com/settings/emails>.

## Step 0 — pre-flight, on the current checkout

Everything below assumes a clean working tree on `main` with all scrubbing merged.

```powershell
git status --porcelain            # must be empty
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe -m mypy src/nox
.venv\Scripts\python.exe scripts/gen_ts_types.py --check
.venv\Scripts\python.exe -m pytest tests/unit -m "not hardware and not network and not spike" -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests/integration -p no:cacheprovider
.venv\Scripts\python.exe scripts/secrets_scan.py --check
.venv\Scripts\python.exe scripts/license_audit.py --check
.venv\Scripts\python.exe scripts/check_links.py
```

And the personal-data check. Build the pattern from *your own* identifiers — real name, nicknames,
handle, e-mail local part and provider, employer or client names, and the absolute paths your
machine uses — then run it over the tracked files. It must print nothing:

```bash
# fill these in locally; do not commit the filled-in version
pattern='yourfirstname|yournickname|youremaillocalpart|@yourmailprovider|YourEmployer'
pattern="$pattern"'|C:\\Users\\|[A-Z]:[\\/](your-data-drive-folder)'

git grep -InIiE "$pattern" -- ':!*.lock' ':!ui/*/package-lock.json' ':!uv.lock'
```

`git grep` only looks at tracked files, which is exactly the set that gets published.

Confirm `LICENSE`, `NOTICE`, `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`,
`CHANGELOG.md` and `.github/**` are all present and current.

## Step 1 — rename the old repository to an archive

The name `Crackxsy/nox` must be free before the new repository can take it.

```powershell
gh repo rename nox-archive --repo Crackxsy/nox --yes
gh repo view Crackxsy/nox-archive --json name,isPrivate,url
```

It stays **private**. Your local `origin` remote still points at the old URL; GitHub redirects it,
but do not push to it again.

## Step 2 — create the new public repository

```powershell
gh repo create Crackxsy/nox --public --description "Local-first AI companion for Windows: desktop pet with voice, Twitch/OBS stream bot, Rocket League coach (observation only) and coding agent." --disable-wiki
```

Do not let it create a README, a license or a `.gitignore` — the snapshot brings its own.

## Step 3 — build the squashed snapshot

Work from the existing checkout. An orphan branch has no parent, so the new history starts at
exactly one commit containing the current tree.

```powershell
git checkout main
git pull --ff-only                      # make sure you snapshot the final state

git config user.name  "Crackxsy"
git config user.email "<id>+Crackxsy@users.noreply.github.com"   # from the lookup above

git checkout --orphan public
git add -A
git status --short | Measure-Object -Line             # sanity: everything you expect, nothing more
git commit -m "feat: Nox v0.1 - local-first AI companion for Windows

Initial public snapshot. The project's private development history (15 commits) is
not carried over; see docs/PUBLISHING.md for why."

git log --format='%H %an <%ae>%n%s'                   # exactly one commit, noreply author
```

If the author line still shows a personal address, stop and fix `git config` before pushing — the
point of the whole exercise is that line.

One known consequence: the compare links at the bottom of `CHANGELOG.md` point at commit hashes
from the old history and will 404 on the new repository. Replace them with tag-based links as part
of the first tagged release (`docs/RELEASE_CHECKLIST.md` item 16), or drop them.

Rename the snapshot branch to `main` and point the remote at the new repository:

```powershell
git branch -M public main-public
git remote rename origin archive
git remote add origin https://github.com/Crackxsy/nox.git
git push -u origin main-public:main
```

Set the default branch explicitly if GitHub did not already:

```powershell
gh api --method PATCH repos/Crackxsy/nox -f default_branch=main
```

## Step 4 — repository settings

Security features first (public repositories get all of these for free):

```powershell
gh api --method PATCH repos/Crackxsy/nox `
  -F security_and_analysis[secret_scanning][status]=enabled `
  -F security_and_analysis[secret_scanning_push_protection][status]=enabled
gh api --method PUT repos/Crackxsy/nox/vulnerability-alerts      # Dependabot alerts
gh api --method PUT repos/Crackxsy/nox/automated-security-fixes  # Dependabot security PRs
```

Then, in **Settings → Code security**, verify by hand that the following are on (some are only
togglable in the UI): **Private vulnerability reporting** (required by `SECURITY.md`), **Dependabot
alerts**, **Dependabot security updates**, **Secret scanning**, **Push protection**.

Merge hygiene, so the linear-history rule below can actually be satisfied:

```powershell
gh api --method PATCH repos/Crackxsy/nox `
  -F allow_merge_commit=false -F allow_squash_merge=true -F allow_rebase_merge=true `
  -F delete_branch_on_merge=true -F has_discussions=true -F has_issues=true
```

Discoverability:

```powershell
gh repo edit Crackxsy/nox --add-topic windows,ai-assistant,desktop-pet,local-first,python,typescript,twitch-bot,obs-websocket,ollama,rocket-league
gh repo edit Crackxsy/nox --homepage "https://github.com/Crackxsy/nox#readme"
```

About text (Settings → the ⚙ next to "About"), if you want to shorten the one set at creation:

> Local-first AI companion for Windows: desktop pet with voice, Twitch/OBS stream bot, Rocket
> League coach (observation only), coding agent. Privacy by design, no telemetry.

Social preview image: **Settings → General → Social preview → Upload an image** (1280×640 PNG).
This one is UI-only; there is no API for it.

## Step 5 — branch protection

Branch protection and rulesets are free on **public** repositories only, which is why this step
comes after the repository is public.

```powershell
pwsh -File scripts/github_branch_protection.ps1 -Repo Crackxsy/nox -Branch develop -DryRun
pwsh -File scripts/github_branch_protection.ps1 -Repo Crackxsy/nox -Branch develop
pwsh -File scripts/github_branch_protection.ps1 -Repo Crackxsy/nox -Branch main -RequiredApprovals 1 -RequireCodeOwnerReview
gh api --method PATCH repos/Crackxsy/nox -f default_branch=develop
# main additionally requires the "Branch policy (main accepts develop only)" status check
# (.github/workflows/branch-policy.yml); add it once the workflow ran at least once.
```

The script requires a pull request, requires the CI jobs as status checks, requires linear history,
and forbids force pushes and deletions. It is idempotent — re-run it after any change to the CI job
names. For a solo maintainer leave `-RequiredApprovals 0`; add `-RequiredApprovals 1
-RequireCodeOwnerReview` as soon as a second maintainer exists.

A required status check only turns green once a job with that exact name has reported at least
once. Push the first pull request before, or immediately after, applying protection.

## Step 6 — verify CI on the public repository

```powershell
gh run list --repo Crackxsy/nox --limit 5
gh run watch --repo Crackxsy/nox
```

All jobs must be green: `Python (ruff, mypy, unit + integration tests)`, `UI (pet)`,
`UI (dashboard)`, `Guard - no input-synthesis / process-memory APIs (Security Model §10)`,
`Release hygiene (third-party license audit, secrets scan)` and
`E2E (Playwright live-core smoke)`.

Then open a throwaway pull request and confirm that merging is blocked until the checks pass — the
protection is only proven once you have seen it block something.

## Step 7 — delete the archive

**Only after step 6 is green.** This is irreversible: the old repository, its history and its
issues are gone.

```powershell
git remote remove archive          # stop pointing at it locally first
gh repo delete Crackxsy/nox-archive --yes
```

Keep one local copy of the old history if you want a personal record of it:

```powershell
git clone --mirror https://github.com/Crackxsy/nox-archive.git nox-history.git   # before deleting
```

Store that mirror somewhere private. It still contains the personal e-mail addresses — that is the
point of keeping it out of GitHub.

## After publication

- Re-run `scripts/secrets_scan.py --check` on the public clone. It scans the full history, which is
  now a single commit; it should stay clean from here on because push protection catches the rest.
- Watch the first Dependabot pull requests; they are the first real test of the protection rules.
- `CHANGELOG.md` and `docs/RELEASE_CHECKLIST.md` drive the v1.0 release from here.
