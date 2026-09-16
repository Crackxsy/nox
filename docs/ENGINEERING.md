# Nox engineering brief

The working brief every contributor (human or agent) follows in this repository. It is deliberately
short; the hard rules below are not negotiable.

Binding sources, in order of precedence:
1. The product interview log (product decisions D0–D256, principle P1) — private product archive, not part of this repository.
2. The Product Requirements Document (PRD 0.1.1, working baseline; approval pending) — private product archive.
3. Architecture contracts (architecture, data model, event model, tool model, security model, ADRs) — private product archive.
4. Code contracts in this repo — the only binding sources an outside contributor needs: `src/nox/core/events.py`, `src/nox/core/state.py`, `src/nox/security/model.py`, `src/nox/ipc/protocol.py`, `src/nox/ai/base.py`, `src/nox/voice/base.py`, plus `SECURITY.md` and `PRIVACY.md` in this folder.

## Environment
- Windows 11, PowerShell or Git Bash. Work from the repository checkout; the project venv is `.venv` (Python 3.13.x from python.org, **not** the Microsoft Store build — see `USER_GUIDE.md` §6).
- Run tools with the venv interpreter explicitly: `.venv/Scripts/python.exe -m pytest tests/unit -q`, `... -m ruff check src tests`, `... -m mypy src/nox/<pkg>`.
- Install extras with `.venv/Scripts/python.exe -m uv sync --extra dev --extra shell [--extra voice]`. `uv.exe` is usually not on PATH, hence `python -m uv`.
- Config defaults: `config/defaults.yaml`. Profiles: `config/profiles/<id>.yaml`. Neither ever contains a real machine path or a secret — the user layer (`%APPDATA%\Nox\user.yaml`) does.
- Runtime data lives under the configured data/vault folders and `%APPDATA%\Nox`, never inside the repository. Tests must use `tmp_path`, never real directories.
- In Python source use raw strings for Windows paths (`r"C:\ProgramData\Nox"`).

## Hard rules (security)
- Kill switch, privacy zones, permission checks and hard prohibitions are enforced in code below the AI layer, never in prompts.
- Rocket League: observation only. Never import or call input-synthesis, memory-reading or injection APIs for game windows — CI fails the build if one appears (the `guard-security-model` job).
- Secrets only via `keyring` (Windows Credential Manager) through `nox.security.secrets`. Never in code, config, Git, logs, prompts, docs or test fixtures (tests use an in-memory backend).
- No telemetry, no outbound calls except user-configured providers/integrations, and only through the egress guard.
- No fake implementations: a feature either works and is tested, or is explicitly reported as missing/degraded. No stubs that silently return success.
- Raw audio is never persisted. Transcripts and memory respect privacy mode/zones.
- No personal data (real names, e-mail addresses, machine paths, employer names) in code, config, tests, fixtures or docs.

## Code conventions
- Python 3.13, `from __future__ import annotations`, pydantic v2 models for all data crossing boundaries, `StrEnum` for enums, asyncio, type hints everywhere (mypy strict), ruff (line length 100).
- Structured logging via `nox.core.logging.get_logger(__name__)` (structlog). Never log secrets, tokens, transcripts or memory content at INFO; use `log.debug` with redaction where needed.
- Dependency injection through constructors; no module-level singletons except the hard prohibition constant.
- Each module gets tests in `tests/unit/<area>/test_*.py` (pytest, `pytest-asyncio` in `asyncio_mode = "auto"`). Security-relevant code needs negative-path tests.
- Keep changes to shared contracts (`events.py`, `state.py`, `model.py`, `protocol.py`, `base.py`) small and additive, and call them out in the pull request.
- TypeScript types under `ui/shared/generated/` are generated from the pydantic models: run `python scripts/gen_ts_types.py` after changing a contract; CI checks they are current.
- Docstrings: one line per module explaining its purpose and which contract/ADR it implements.

## Contributing
Branch and pull-request flow: `../CONTRIBUTING.md`. Reporting a vulnerability: `../SECURITY.md`.

## Report format for project-internal agents — keep it short
```
## Result      1–3 sentences
## Changed     files, one half-sentence each (contract changes marked)
## Tests       command → result (summary line only)
## Open Points only genuine ones (product decisions, blockers)
```
Project-internal agents start from the context map in the private product archive and the
`CP-<Area>.md` package for their area, then open only the REQUIRED sources listed there.
Measurements, decisions and gotchas that matter beyond a single task go into the product archive
(spike note, ADR, context package), not only into the report.
