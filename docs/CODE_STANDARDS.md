# Code and design standards

This is the bar every change is reviewed against before it reaches `develop`, and again before a
release reaches `main`. It is written for a reader who has never seen the project: if a rule needs
project lore to make sense, the rule is wrong.

## 1. Readable as if written by one person

- One style across all modules. Names say what a thing does (`replay_backfill_queue`, not `q2`).
- Comments explain *why*, never *which ticket*. Story, spike and requirement identifiers (`ST-18-03`,
  `SP-07`, `FR-7.16`, `B-11`, `OP-7 C`) belong in the changelog and the private product archive, not
  in code comments or docstrings. If a constraint matters, state the constraint.
- A module has a one-paragraph docstring saying what it owns and what it deliberately does not.
- No dead code, no commented-out code, no "temporary" flags older than one release.

## 2. Small and single-purpose

- A function does one thing and fits on a screen. A file has one reason to change; files above
  ~600 lines are split along responsibilities (composition root, handlers, boot sequence, ...).
- No sentinel tricks where a proper type works (`Optional` with a documented default, a dataclass,
  a small helper). No `getattr(obj, "x", default)` to paper over missing wiring.
- Boot and request paths never block the event loop: CPU work goes to a thread, I/O is awaited,
  long scans yield.

## 3. Errors are explicit

- No silent `except Exception: pass`. Either handle the case with a real fallback, or log it with
  context (`event`, the identifiers needed to find it again) and degrade honestly (`limited` /
  `unavailable` with a reason a user can act on).
- Users never see tracebacks. Every user-facing error says what happened and what to do next.
- Availability is never faked: a subsystem that cannot work says so.

## 4. Typed and tested

- `mypy` clean; public functions fully annotated; no `Any` where a type is known.
- Tests are deterministic: injected clocks instead of sleeps, OS-granted ports instead of random
  ones, fakes instead of real hardware or network. A test name states the behaviour it protects.
- New behaviour comes with a test; a fixed bug comes with a test that fails before the fix.

## 5. Documented at the surface

- Every public function, class, IPC request, event and config key has a docstring or a table entry.
- `README.md`, `docs/` and the code agree. The changelog gets one line per user-visible change.

## 6. Security below the model, privacy by design

- Kill switch, permissions, privacy modes and the egress guard are enforced in code, never by a
  prompt. Every outbound request goes through the guard; every consequential action is audited.
- Secrets live in the Windows Credential Manager only. No personal names, e-mail addresses,
  machine paths or private project names anywhere in the repository.

## 7. UI and UX

- One design system: shared tokens (`ui/shared/tokens.css`), a type scale, a spacing scale, both
  themes designed with equal care (light and dark are two palettes, not an inversion).
- Every state is designed: empty, loading, error, success, offline. No placeholder text in shipped
  screens; honest status instead ("Kern nicht erreichbar", not "Loading...").
- Accessible: WCAG AA contrast, full keyboard operation, visible focus, labels on every control,
  ARIA roles where semantics need them.
- Responsive from 360 px up without horizontal scrolling. Copy is plain German for users and plain
  English for developers; labels are words, never config paths or identifiers.
- Self-explaining: a first-time user finishes setup from the screens alone; help appears where the
  decision is made, not in a separate document.

## 8. Before you say "done"

- Run the full verification (lint, types, unit, integration, e2e, UI tests and builds, license
  audit, secrets scan, link check) and quote the results.
- Read your own diff as a strict reviewer would. State honestly what is proven, what is untested,
  and what is a known limitation.

## 9. Adopted practices

Distilled from the MIT-licensed [Everything Claude Code](https://github.com/affaan-m/everything-claude-code)
rule set and adapted to Nox:

- Prefer early returns over nested conditionals; name every threshold, delay and limit as a
  constant with a unit in its name (`RECONNECT_BACKOFF_MAX_S`).
- Prefer values over mutation: return updated copies (pydantic `model_copy`, frozen dataclasses,
  spread in TypeScript) unless a hot path demands in-place updates, and say so in a comment.
- Tests follow Arrange–Act–Assert and are named after the behaviour they protect
  ("falls back to FTS5 when Ollama is unreachable"). Coverage target: 80 % of statements across
  unit, integration and end-to-end tests; a change that lowers coverage explains why.
- Before every commit: no hard-coded secrets, every external input validated at the boundary
  (pydantic payloads on IPC, manifests, config), SQL only with parameters, rate limits on every
  request surface, error messages that never leak paths, tokens or internal identifiers.
- Review severity is stated on every finding (critical / high / medium / low) with the concrete
  failure scenario, and a review without a failure scenario is not a finding.
