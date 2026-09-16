<!-- Base branch: develop (main only takes release pull requests from develop). -->
<!-- Thanks for contributing to Nox. Keep one logical change per pull request. -->

## What and why

<!-- What changes, and what problem it solves. Link the issue: Closes #123 -->

## How it was verified

<!-- The commands you ran and what you saw. "CI will tell me" is not a verification. -->

```
```

## Checklist

- [ ] One logical change; the title follows [Conventional Commits](https://www.conventionalcommits.org/) (`feat(scope): ...`, `fix(core): ...`, `docs: ...`).
- [ ] Tests added or updated, and they fail without the change.
- [ ] Security-relevant code has a negative-path test (the case that must be rejected).
- [ ] `CHANGELOG.md` updated under `## [Unreleased]`.
- [ ] Documentation updated (`README.md`, `docs/**`) if behaviour or setup changed.
- [ ] Local checks pass: `ruff check`, `ruff format --check`, `mypy src/nox`, `pytest tests/unit`, `pytest tests/integration`, `scripts/gen_ts_types.py --check`, and `npm test` in any UI you touched.

## Security rules (see CONTRIBUTING.md)

- [ ] No input-synthesis or process-memory API for a game window — games stay observation-only.
- [ ] No secret, key, token or password in code, config, tests, fixtures, logs, prompts or docs.
- [ ] Every new outbound connection goes through the egress guard and a profile allowlist entry.
- [ ] No telemetry of any kind was added.
- [ ] No personal data (real names, e-mail addresses, employer names, machine paths) — placeholders only.
- [ ] No stub that silently reports success; unavailable features report `limited`/`unavailable` with a real reason.

## Anything a reviewer should know

<!-- Trade-offs, follow-ups, deliberately-out-of-scope items, open questions. -->
