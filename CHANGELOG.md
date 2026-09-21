# Changelog

All notable changes to Nox are documented here, in [Keep a Changelog](https://keepachangelog.com/)
style. Versioning follows [Semantic Versioning](https://semver.org/) (FR-11.17). Every release is
tagged; changelog entries are reviewed before merge, not added after the fact (also FR-11.17).

Nox is pre-v1.0 (private, unreleased) as of this writing — the entries below document what shipped
internally, backfilled from the git history, so the process is in place before the first public
release. See `docs/RELEASE_CHECKLIST.md` for what v1.0 itself requires.

## [Unreleased]

### Added (2026-09-16, pet window — design tokens, sprite variant, live variant switch)
- One palette for both front-ends: the dashboard's Apple-style token block now lives in
  `ui/shared/tokens.css` (same names, same values, same `prefers-color-scheme` +
  `data-theme` guards) and the pet window uses it instead of its own dark-only colours. The pet
  therefore follows the OS between light and dark — chips, notes, the status label and the
  capture indicator all do — while the window itself stays transparent, and each creature variant
  finally uses the body lightness it already shipped for the active background (#25).
- The pet can render a **sprite variant**: `pet.variant: sprite:<id>` loads
  `variants/<id>/sprites.json`, validates it, preloads every frame and plays the frame sequences
  the existing state machine selects (idle / listening / speaking / thinking / sleeping, plus
  optional blink), with a 150 ms crossfade between expressions, idle breathing as a subtle
  scale/translate, `prefers-reduced-motion` honoured, and a logged fallback to the procedural
  variant when a manifest or a single frame fails to load. A generated placeholder set ships under
  `variants/placeholder/` so the path works end to end before the real art exists (#19).
- `config.set pet.variant` takes effect without restarting Nox: the shell reloads the pet page with
  the new variant on `settings.changed`, and the pet page subscribes to the same event so it can
  swap the variant in place once the core exposes the value to the `pet` role (#24).

### Added (2026-09-16, voice: Kokoro TTS and a wake-word gate)
- Kokoro (`kokoro-onnx`) as a second text-to-speech engine next to Piper, selectable with
  `voice.tts.engine: kokoro` and the new optional extra `voice-kokoro` (#21). Same `TtsEngine`
  contract as Piper: sentence-wise streaming, synthesis off the event loop, reported sample rate
  (24 kHz), prepared clips. Model files are resolved from `<data_dir>/models/kokoro/` and are never
  downloaded automatically - a missing file is an `unavailable` health reason that names the path
  and the command, `python -m nox.worker --download-kokoro` (~354 MB, user-initiated, no core and
  no egress guard involved). The default stays `piper`.
- Wake-word gate in front of Whisper (#20): `voice.stt.wake_word_engine: openwakeword|text`,
  `wake_word_model`, `wake_word_threshold`, `wake_window_s` and `conversation_window_s`. While
  push-to-talk is not held and no conversation window is open, a speech segment only reaches
  Whisper if the cheap always-on detector fired - background audio (a video, a game) is dropped
  before transcription instead of filling the queue with 20-40 s of latency and inventing
  languages in noise. Audio the user did not direct at Nox is now never transcribed at all.
- `voice.stt.listening_mode: continuous|ptt_only` (#20). `ptt_only` never opens the microphone
  unless push-to-talk is held; `continuous` stays the default.
- `voice.stt.kill_phrase_watchdog` / `kill_watchdog_max_ms` (#20): because no acoustic model for
  the kill phrase exists, short segments still reach Whisper so "Nox Notaus" keeps working. Such a
  transcript is checked for the kill phrase and then discarded - it never becomes an event and
  never reaches a language model.
- `voice.models_dir`: one root for the Piper, Kokoro and openWakeWord model files (empty =
  `<paths.data_dir>/models`).

### Changed (2026-09-16, voice)
- Voice model and clip directories are derived from the data directory instead of being hard-coded
  absolute paths in the engine modules (#21). **Action required for existing installations whose
  models are not below `paths.data_dir`:** set `voice.models_dir` in `user.yaml` (or move the
  `piper/`, `faster-whisper/` folders), otherwise the engines look under
  `%APPDATA%\Nox\models` and report their models as missing.
- `voice.tts.engine` is validated against `piper|kokoro` instead of being a free-form string.
- `openwakeword` was added to the `voice` extra; clip handling shared by both TTS engines moved to
  `nox.voice.tts.clips`.
- `NOTICE` and `docs/license_policy.yaml` record the honest license status of the Kokoro route
  (#21): Kokoro's own code, ONNX Runtime and the model files are permissive, but `kokoro-onnx`
  still depends on `phonemizer` and espeak-ng (GPL-3.0-or-later), so switching to Kokoro moves the
  GPL obligation on bundled builds rather than removing it. Reaching an Apache-2.0-only installer
  needs a permissively licensed grapheme-to-phoneme front end and remains an open decision.

_No git tags exist yet; they will be created at the first public release (`docs/PUBLISHING.md`) -
until then the compare links below point at tags that don't exist yet either._

### Fixed (2026-09-16, `user.yaml` lost hand-written comments on every write — #22)
- `nox.settings.layers.write_user_config` — the one writer behind `config.set` and `nox onboard` —
  round-trips the User layer through `ruamel.yaml` instead of `yaml.safe_dump`: comments, key
  order and quoting style survive a settings change, and only the keys in the patch change. What
  is written stays plain YAML; every reader still parses it with `yaml.safe_load`. A `user.yaml`
  that does not parse now raises `UserConfigError` and is left untouched instead of being quietly
  replaced by a fresh document. New core dependency `ruamel.yaml` (MIT).

### Added (2026-09-16, the dashboard asks for the PIN on a secret change — #23)
- New read-only `security.pin.status {}` -> `{configured}` (roles `shell`/`dashboard`): presence
  only, never the PIN, its hash, its length or its algorithm. The core has required a `pin` on
  `secrets.set`/`secrets.delete` ever since a PIN is configured, but the Settings page never sent
  one, so every credential change failed with "permission denied". The page now shows an inline
  PIN field next to Speichern/Löschen when a PIN is configured, sends it with exactly that one
  request, clears it afterwards and stores it nowhere, and turns a PIN refusal into the honest
  `pin_required`/`pin_wrong` message instead of a raw error line.

### Changed (2026-09-16, the Twitch bot's knobs are configuration, not manifest-only — #26)
- `stream.twitch` gained `bot_names`, `relevance_cooldown_s`, `rate_limit_max_messages`,
  `rate_limit_window_s`, `rate_limit_min_gap_s`, `min_backoff_s` and `max_backoff_s` — typed,
  validated, with the manifest's own values as defaults — and all of them are editable on the
  Settings page with de/en labels. The plugin reads them from the configuration
  (`nox_plugin_twitch.settings.resolve_settings`); a manifest that still carries one of the moved
  keys keeps working for one release and logs `twitch.manifest_config_deprecated`.

### Fixed (2026-09-16, RL Stage 1 capture had no privacy gate — #27)
- The Stage 1 HUD `_recognize_loop` (`plugins/rl/src/nox_plugin_rl/plugin.py`) captured the
  screen on a timer regardless of privacy state, unlike Stage 2's `_vision_loop`, which already
  stops while a privacy zone is active or `privacy.capture.screen` is off (ST-18-03 AC2's
  `_capture_allowed` gate). New `nox.rl.capture_gate.CaptureGate`: a `privacy.capture_changed`-
  driven gate (reusing `PrivacyService.effective_capture()`'s `screen` boolean - already accounts
  for zone/mode/capture-flag/panic/safe-mode) meant to back both loops so neither hand-rolls its
  own privacy check; logs `rl.capture_paused reason=...` / `rl.capture_resumed` exactly once per
  transition and guarantees a paused loop grabs no frame at all via `maybe_capture()`.

### Added (2026-09-16, persisted proactive notifications — #28)
- `nox.proactive.store.NotificationStore` (ST-19-08) is now backed by a `proactive_notifications`
  table (migration `0010_notifications`) when constructed with a database, so notification history
  survives a restart; `db=None` keeps the original in-memory ring buffer for tests. New
  `NotificationRepository` (`nox.data.repos`). New `proactive.notification.dismiss {id}` tool,
  persisted like everything else in the store; expired/dismissed rows are pruned by
  `NotificationRepository.purge_expired` (module-level `DEFAULT_NOTIFICATIONS_RETENTION_DAYS = 30`
  default - see the report to the config owner about adding
  `proactive.notifications_retention_days` to `ProactiveConfig`).

### Fixed (2026-09-16, proactive service silently discarded an injected notification store)
- `ProactiveService.__init__` used `store or NotificationStore(...)`; since `NotificationStore`
  defines `__len__`, a caller-supplied store that was still empty (0 records) was falsy and got
  replaced with a fresh, unrelated in-memory store - notably `nox.proactive.install.install`'s
  now-db-backed store right after boot. Fixed to an explicit `is not None` check.

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

No tags exist yet (repo is pre-v1.0/private): the links below used to point at commit hashes from
this pre-publication history, which 404 on the public repository (#29) - they now use the
tag-based form `docs/PUBLISHING.md`'s squashed-snapshot publish is expected to create tags for
(item 16 of `docs/RELEASE_CHECKLIST.md`), even though those tags do not exist yet either; both
`v0.1.0` and `v0.2.0` get an actual release tag at first publish, `v0.0.0` ("Repository
foundation") predates the tagging process and never gets one, hence the single-tag link shape
below instead of a compare for it and for `v0.1.0` (nothing tagged to compare either against).

[Unreleased]: https://github.com/Crackxsy/nox/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Crackxsy/nox/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Crackxsy/nox/releases/tag/v0.1.0
[0.0.0]: https://github.com/Crackxsy/nox/releases/tag/v0.0.0
