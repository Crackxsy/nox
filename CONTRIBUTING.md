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

### What a test has to prove

These rules hold for every test, and they are checked most carefully on tests an AI agent wrote,
because those tend to be numerous and shallow:

- **Every policy list is tested element by element.** A deny list, an allow list or a set of
  forbidden values gets one test case per element, written out as literals. Parametrizing over the
  constant itself proves nothing: remove an element and its test case disappears with it.
- **Every `except`, default and limit has a test that breaks it.** For each exception handler,
  fallback value, cap or timeout there is a test that drives the code into it and asserts what must
  happen there - the error is reported, the default is the documented one, the cap holds at its
  exact value.
- **A bug fix comes with a test that fails without the fix.** Say in the pull request that you saw
  it fail.
- **No sleep-then-assert.** Wait for an event, a condition with a timeout, or call the step
  directly; a test that passes only because a `sleep` was long enough is flaky by design.
- **At least one behavioural assertion per test.** `assert result is not None` or "it did not
  raise" is not a test of behaviour; assert the value, the state change or the message.
- **A new security test must demonstrably kill a mutant.** Change the guarded line (drop an element,
  flip a comparison, change a default) and show that the test fails; mention the mutant in the pull
  request.
- A bare `pytest` must stay hermetic: anything that needs a real service, device, network or toolchain
  is marked `e2e`, `network`, `hardware` or `spike`.

## This is a public repository

Everything committed here - code, comments, tests, fixtures, docs, commit messages and pull-request
text - is public:

- **English** for code, comments, docs, commits and pull requests (user-facing UI strings are
  translated through the i18n files).
- **No personal data**: no real names, places, e-mail addresses, employers, or details of anyone's
  machine, hardware or accounts. Use neutral examples (`Alex`, `Springfield`, `example.com`).
- **No local paths**: no drive letters or home folders from a real machine. Use `tmp_path` in tests,
  placeholders in docs and command-line arguments in scripts.
- **Commit with your GitHub noreply address** (`<id>+<user>@users.noreply.github.com`), never a
  private one.
- No internal planning IDs (story, decision or open-point numbers) in code comments: say what the
  rule is, not where it was decided.

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
   The `release-hygiene` CI job scans the working tree *and* the history: every commit a pull
   request adds (a secret added and deleted again is still found), the whole branch on a push.
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
