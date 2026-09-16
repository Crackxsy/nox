# Changelog

All notable changes to Nox are documented here, in [Keep a Changelog](https://keepachangelog.com/)
style. Versioning follows [Semantic Versioning](https://semver.org/) (FR-11.17). Every release is
tagged; changelog entries are reviewed before merge, not added after the fact (also FR-11.17).

Nox is pre-v1.0 (private, unreleased) as of this writing — the entries below document what shipped
internally, backfilled from the git history, so the process is in place before the first public
release. See `docs/RELEASE_CHECKLIST.md` for what v1.0 itself requires.

## [Unreleased]

### Added (2026-09-16, editable settings — EPIC-21)
- Settings are writable, not just readable (PO: "in den Einstellungen kann man nur den Status
  lesen"). New `nox.settings` package with `config.get`/`config.set` over an explicit allow-list
  of configuration paths whose types, options and bounds are derived from the pydantic models in
  `nox.core.config`; values are validated before anything is written, written to the User layer
  (`user.yaml`) by the same writer `nox onboard` uses, live-applied where that is genuinely
  possible and honestly reported as `restart_required` otherwise. New `settings.changed` event
  (paths only, never values).
- `secrets.status`/`secrets.set`/`secrets.delete`: manage the six known credential names without
  ever showing a value. Rate-limited, PIN-gated when a PIN is configured, audited by name.
  There is deliberately no `secrets.get`.
- Twitch login by OAuth 2.0 Device Code Grant (`twitch.auth.start`/`status`/`disconnect`, event
  `twitch.auth.changed`) instead of pasting an `oauth:` token: a public client, `chat:read
  chat:edit` only, tokens straight into the keyring, every call through the egress guard, with a
  background refresh so a long stream does not die of an expired token. `id.twitch.tv:443` is
  allow-listed in the defaults and in the `companion`/`stream` profiles, and documented in
  `docs/PRIVACY.md`.
- The personality moved out of the source tree: the repository ships a neutral default and each
  installation keeps its own `<data_dir>/personality.md`, created on first start, re-read on
  every change, and editable through `personality.get`/`personality.set`. The operating rules
  stay in code.
- `stream.twitch.channel` in the configuration, so the Twitch channel is an editable setting; the
  plugin still prefers an explicit `channel` in its own manifest.

### Fixed (2026-09-16, cold start after a reboot)
- Supervisor counted missed heartbeats from the moment it spawned the core, so a cold boot
  (20-40 s of imports and index work) looked like a hang: it asked for a graceful restart before
  the core had even connected. Accounting now starts with the core's first heartbeat and never
  before a boot grace of 90 s has passed (`SupervisorSettings.boot_grace_s`, read from
  `supervisor.boot_grace_s` when the config schema carries it); inside the grace only the hard
  liveness check applies (process exited -> restart).
- `sup.kill mode=restart` (the watchdog's graceful restart request) engaged the kill switch: Nox
  ended up in safe mode with the voice worker terminated and nothing restarted. A restart request
  is now a clean shutdown (exit 0) that the supervisor answers by respawning the core; kill switch,
  panic and `mode=safe_mode` are unchanged.
- Boot blocked the event loop for ~10 s (extension modules imported synchronously - `rl` pulls in
  OpenCV - and the `pm` extension indexed the whole vault inline), so no heartbeat could go out.
  Extension modules are imported in a worker thread, the pm index runs as a background task
  (`pm` health reports `limited` until it finishes), the database is opened off the loop, and the
  supervisor client now starts before any of it.
- First vault scan embedded one chunk per Ollama request; chunks are now batched per note (at most
  32 per request) and the sqlite-vec upserts run off the loop.
- `ai.providers` could exceed the UI's 10 s request timeout because it probes every provider live
  (`claude_code` alone may take 15 s), leaving the dashboard's provider card empty while health
  showed the same providers: the probe now has a 3 s budget and falls back to the health service's
  last observation instead of returning nothing.

### Fixed (2026-09-15, first real start after onboarding)
- Core froze after `core.started`: the memory vault scan blocked the event loop — one fresh SSL
  context (~250 ms of CA-bundle loading) per embedding request, and no yield at all on already
  indexed notes. Heartbeats stopped, the supervisor restart-looped the core into safe mode.
  Now: one shared SSL context per process (`nox.security.egress.shared_ssl_context`), the
  memory extension's Ollama client goes through the egress guard (B-11) like the chat provider,
  and the indexer yields after every chunk and note.
- Voice worker died on a single 5 s handshake timeout while the core was still booting; it now
  retries with backoff for up to 60 s.
- Shell stayed offline for the whole session once the first bridge connect failed, and could
  not follow a supervisor-restarted core (stale session token). It now re-reads the runtime
  files, retries every 3 s, drops a bridge after two failed pings, and reloads the pet page.
- `pm` extension refused to start because `08 - Epics/Epics Overview.md` (a map note) was
  parsed as an epic; only `EPIC-nn`/`ST-nn-nn` file names are work items now.
- `nox doctor` warns when the venv runs on Microsoft-Store Python (virtualized `%APPDATA%`,
  the cause of `token_acl_failed`).
- First voice turn deadlocked the worker connection (hub delivered the worker's event inline,
  the orchestrator awaited LLM + TTS inside the handler, TTS waited for `tts.finished` from the
  blocked socket): voice turns run as a task, the hub pumps inbound events per client off the
  receive loop, the worker reconnects and re-registers, the core forgets a dropped worker, the
  shell reloads the pet page when the session token changed.

### Added (2026-09-16, public-release preparation)
- License decided (OP-D closed): Apache-2.0. `LICENSE` (full text) and `NOTICE` added,
  `LICENSE-PENDING.md` removed; `pyproject.toml` and both `ui/*/package.json` declare
  `Apache-2.0`. The optional `voice` extra pulls in GPL-3.0 components, so a bundled build
  (e.g. the installer) ships under GPL-3.0 terms as a combined work - stated in `NOTICE` and
  accepted as a written exception in `docs/license_policy.yaml`.
- Governance and community files for a public repository: `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, root `SECURITY.md`, `.github/CODEOWNERS`, `.github/dependabot.yml`,
  pull-request and issue templates, `docs/PUBLISHING.md`, `scripts/github_branch_protection.ps1`
  and `scripts/check_links.py`.

### Changed
- Repository prepared for publication: `README.md` rewritten for readers who do not know
  the project, and all personal data (names, e-mail addresses, employer references,
  machine-specific paths) replaced with placeholders across config, plugins, tests, spikes
  and docs.
- Dashboard redesigned (PO: "unübersichtlich, KI-Slop"): design system in `styles.css` with
  light and dark palettes (system preference plus System/Hell/Dunkel toggle, `nox.theme`),
  sidebar navigation, cards, status pills, chat bubbles, macOS-Settings-style grouping. ARIA
  structure, i18n keys and the e2e selectors are unchanged.

### In progress
- Rocket League coach, Stage 1 (Spec v0.3, EPIC-12) — observation-only detection, replay pipeline;
  config scaffolding (`rl:` section) landed, feature work ongoing.
- v1.0 public release readiness (Spec v1.0, EPIC-21): license audit, secrets scan, onboarding
  wizard CLI, documentation set, external security review readiness — this changeset.

## [0.2.0] - 2026-09-14

Stream companion (Spec v0.2, EPIC-11): Twitch chat, OBS scene control, and the Funken
viewer-loyalty system, on top of the v0.1 walking skeleton.

### Added
- Plugin runtime: manifest-validated worker processes, tool registry/executor, per-plugin scoped
  egress guard (ADR-012/ADR-013).
- OBS Studio plugin (obs-websocket v5 over loopback): read-only status/scene inventory, one
  confirmed scene switch, a privacy-scene safety action, a read-only preflight check — no
  scene/source-delete or stream-stop tool exists at all.
- Twitch plugin (IRC over TLS): chat responder, moderation gate, rate limiting.
- Stream data model, event types, and the Funken ledger (viewer currency/loyalty).
- Stream session service, Funken booking, dashboard Stream page.

### Fixed
- IPC client fix (stream bot core changeset).

## [0.1.0] - 2026-09-11

v0.1 walking skeleton: the first end-to-end run of Nox's core process, security layer, IPC hub,
AI router, voice worker, supervisor, and desktop shell/dashboard UIs.

### Added
- Core process lifecycle, config layering (Defaults → User → Profile → Runtime Override,
  ADR-010), event bus, state manager, health checks.
- Security layer: permission engine, privacy modes/zones, kill switch, audit log
  (hash-chained), secrets store (`keyring`-backed), profiles.
- Local IPC hub (WebSocket, versioned, per-client tokens, role-based).
- AI provider abstraction and router (Claude Code, Ollama, a deterministic rules fallback) with a
  degradation chain.
- Voice worker: local speech-to-text (faster-whisper) and text-to-speech (Piper), push-to-talk and
  wake-word support.
- Supervisor: watchdog, kill-switch hotkey path independent of the core process.
- PySide6 desktop shell (pet window, tray, hotkeys) and the initial React/TypeScript pet renderer
  and dashboard.
- `nox` CLI (`core`, `shell`, `supervisor`, `dev`, `doctor`, `secrets set/delete/check`).

### Security hardening (same release window)
- Graceful supervisor shutdown, speech gate (no unsolicited speech in private modes), typed chat
  stream, pet variant scaffolding.

## [0.0.0] - 2026-09-09

Repository foundation: package layout, `config/defaults.yaml`, and the interface contracts other
modules build against (`events.py`, `state.py`, `model.py`, `protocol.py`, `base.py`). No running
product yet.

No tags exist yet (repo is pre-v1.0/private) - links below use commit hashes, not tags, and will
move to tag-based compare links once releases are actually tagged (item 16 of
`docs/RELEASE_CHECKLIST.md`).

[Unreleased]: https://github.com/Crackxsy/nox/compare/54ce00e...HEAD
[0.2.0]: https://github.com/Crackxsy/nox/compare/8584d5c...54ce00e
[0.1.0]: https://github.com/Crackxsy/nox/compare/b3c363e...8584d5c
[0.0.0]: https://github.com/Crackxsy/nox/commit/b3c363e
