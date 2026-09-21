# Contributing to Nox

Thanks for taking a look. Nox is a Windows-only, local-first desktop companion; the parts of it
that touch a microphone, the screen, a game window or the network have hard rules that a pull
request cannot relax. Everything else is open to discussion.

Before a larger change, open an issue or a discussion first — the roadmap is opinionated and it is
better to agree on the shape of a feature before you build it.

## Development setup

Requirements: Windows 11, **Python 3.13 from python.org** (not the Microsoft Store build — Store
Python virtualises `%APPDATA%`, which breaks Nox's runtime token files), Node.js 20 or newer, Git.

```powershell
git clone https://github.com/Crackxsy/nox.git
cd nox
python -m pip install uv
python -m uv sync --extra dev --extra shell        # add --extra voice / --extra rl / --extra e2e as needed
.venv\Scripts\python.exe -m nox.cli doctor         # honest environment report
```

Build the UIs when you touch them (the core serves the built bundles):

```powershell
cd ui\pet       && npm ci && npm run build
cd ui\dashboard && npm ci && npm run build
```

## Before you open a pull request

Run what CI runs:

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe -m mypy src/nox
.venv\Scripts\python.exe scripts/gen_ts_types.py --check
.venv\Scripts\python.exe -m pytest tests/unit -m "not hardware and not network and not spike" -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests/integration -p no:cacheprovider
cd ui\pet && npm test && cd ..\dashboard && npm test
```

Tests are required. A behaviour change without a test will not be merged, and security-relevant
code needs negative-path tests (the case that must be *rejected*), not only the happy path.

## Branches, commits, pull requests

- Two long-lived branches: **`develop`** (default; integration, every contribution targets it) and
  **`main`** (release; only receives pull requests from `develop`, opened by the maintainer).
- Branch off `develop`: `feat/<short-topic>`, `fix/<short-topic>`, `docs/<short-topic>`, `chore/...`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/), as the
  existing history does: `feat(stream): ...`, `fix(core): ...`, `docs: ...`, `ci: ...`,
  `test: ...`, `chore: ...`. Write what changed and why, in the imperative.
- Both branches are protected: changes land through a pull request with green CI and a linear
  history (rebase or squash, no merge commits, no force-push). `main` additionally requires a
  code-owner review and the "Branch policy" check, which only passes for `develop` → `main`.
- One logical change per pull request. Fill in the pull-request template, including the
  `CHANGELOG.md` entry under `## [Unreleased]`.
- **No DCO sign-off and no CLA are required.** By contributing you agree your contribution is
  licensed under the Apache License 2.0 (see [`LICENSE`](LICENSE)), as
  [Apache-2.0 §5](LICENSE) already states.

## Security rules a pull request must respect

These are enforced in code and in CI, below the AI layer — a prompt, a config option or a plugin
manifest can never turn them off. A pull request that weakens one of them will be closed.

1. **Games are observation-only.** Never import or call an input-synthesis or process-memory API
   (`SendInput`, `pyautogui`, `pydirectinput`, `ReadProcessMemory`, `WriteProcessMemory`, …) for a
   game window. The `guard-security-model` CI job greps `src/` and `plugins/rl/` and fails the
   build if one appears.
2. **Secrets never live in code.** No key, token, password or OAuth secret in source, config,
   tests, fixtures, logs, prompts or documentation. Secrets go through
   `nox.security.secrets` into the Windows Credential Manager; tests use the in-memory backend.
   The `release-hygiene` CI job scans the working tree *and* the full git history.
3. **All egress goes through the guard.** Every outbound connection goes through
   `nox.security.egress` and the profile's allowlist. No library may open its own socket to the
   internet, and no new default endpoint may be added without a profile entry.
4. **No telemetry, ever.** Nox does not phone home — not anonymised, not opt-out, not "just crash
   reports". There is no code path to add.
5. **No personal data.** No real names, e-mail addresses, employer names or machine-specific paths
   in code, config, tests, fixtures or docs — use placeholders (`alex@example.org`,
   `%USERPROFILE%/Projects`).
6. **No fake capabilities.** A feature either works and is tested, or it reports itself as
   `limited`/`unavailable` with a real reason. A stub that silently returns success is a bug.
7. **Privacy guarantees hold in code.** Raw audio is never persisted; capture, screenshots,
   clipboard reads and memory writes stop while a privacy zone is active; the capture indicator
   cannot be hidden by configuration.

See [`docs/ENGINEERING.md`](docs/ENGINEERING.md) for the code conventions,
[`docs/CODE_STANDARDS.md`](docs/CODE_STANDARDS.md) for the readability/design bar every change is
reviewed against, and [`docs/SECURITY.md`](docs/SECURITY.md) for the model these security rules
come from.

## Plugins

Plugins are the intended extension point and live under `plugins/<name>/` with a manifest that
declares their tools, secrets and network egress. Read
[`docs/PLUGIN_AUTHORING.md`](docs/PLUGIN_AUTHORING.md) first — a plugin cannot request a
hard-prohibited capability, and the manifest validation will reject it if it tries.

## Reporting a vulnerability

Do not open a public issue. Follow [`SECURITY.md`](SECURITY.md).

## Code of conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
