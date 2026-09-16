# Nox

[![CI](https://github.com/Crackxsy/nox/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Crackxsy/nox/actions/workflows/ci.yml)
[![Status: pre-1.0](https://img.shields.io/badge/status-pre--1.0-orange)](CHANGELOG.md)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Platform: Windows 11](https://img.shields.io/badge/platform-Windows%2011-0078d4)](#requirements)
[![Python 3.13](https://img.shields.io/badge/python-3.13-3776ab)](#requirements)

A local-first AI companion that lives on your Windows desktop — one entity with a stable
personality, not a chat window with a mascot on top.

> **Kurzfassung (Deutsch):** Nox ist ein lokaler KI-Begleiter für Windows: ein Desktop-Pet mit
> Stimme, ein Twitch-/OBS-Streambot, ein Rocket-League-Coach (nur Beobachtung, niemals
> Eingaben ins Spiel) und ein Coding-Agent. Alles läuft so weit wie möglich auf dem eigenen
> Rechner, es gibt keine Telemetrie, und Kill-Switch, Privacy-Modi und Berechtigungen sind
> **unterhalb** des Sprachmodells in Code durchgesetzt. Die Oberfläche ist deutsch, die
> Dokumentation englisch. Nox ist vor Version 1.0 — die Liste unten sagt ehrlich, was heute
> funktioniert.

## What Nox is

- **Desktop pet** — a small always-on-top window with a mood, a personality, and a local voice
  (speech-to-text and text-to-speech run on your machine).
- **Twitch / OBS stream companion** — chat responder, a "Funken" viewer loyalty currency, and a
  deliberately narrow OBS tool surface (scene switching is a confirmed action; there is no
  scene-delete and no stream-stop tool at all).
- **Rocket League coach** — **observation only**: screen/HUD capture, game audio and replay files.
  Nox never sends input to the game and never reads or writes its process memory. This is a
  code-level boundary enforced by a CI job, not a setting you could flip.
- **Coding agent** — orchestrates the Claude Code CLI inside workspaces you configured, under the
  permission engine.
- **Project/research/creative assistant** — partly built, see the honest status list below.

Design principles: one coherent entity rather than a chatbot with a pet skin; local first with no
hard cloud dependency; security before autonomy; no fake capabilities (every subsystem reports
`available` / `limited` / `unavailable` with a real reason); recoverable by design (supervisor,
degraded modes, rollback).

## What works today (60-second version)

Nox is **pre-1.0**. Honest state as of the latest release:

| Area | State |
| --- | --- |
| Core, supervisor, kill switch, privacy modes/zones, permission engine, audit chain | Works |
| Local IPC hub, desktop shell (pet window, tray, hotkeys), dashboard | Works |
| Voice: push-to-talk, wake word, local STT (faster-whisper) + TTS (Piper or Kokoro) | Works, needs the `voice` extra and a model download; see [Voice](#voice-microphone-and-speech) |
| AI routing: Claude Code CLI → Ollama → deterministic rules fallback | Works; each provider reports its real health |
| Memory: SQLite + `sqlite-vec`, vault indexing | Works; first start indexes the whole vault once |
| Twitch chat bot, OBS scene control, Funken ledger, dashboard Stream page | Works (stream profile only) |
| Telegram bridge | Works once a bot token is set |
| Rocket League coach | Stage 1: observation and replay pipeline; coaching quality is early |
| Coding agent (Claude Code orchestration) | Works inside configured workspaces; still rough |
| Clips, creative-app detection, mobile companion | Early, partly scaffolding |
| Signed installer, auto-update, rollback | Not proven yet — run from source for now |

`nox doctor` prints the same picture for *your* machine, including why something is unavailable.
See [`CHANGELOG.md`](CHANGELOG.md) for what shipped when.

## Requirements

- **Windows 11.** Nox is Windows-only by design (Qt shell, Windows Credential Manager, per-window
  privacy zones). There is no Linux or macOS build and none is planned.
- **Python 3.13 from [python.org](https://www.python.org/downloads/)** — *not* the Microsoft Store
  build: Store Python virtualises `%APPDATA%`, so Nox's runtime token files end up somewhere other
  tools cannot read and startup fails with `token_acl_failed`.
- **Node.js 20 or newer** to build the two UIs (the core serves the built bundles).
- **[Ollama](https://ollama.com) (optional)** for fully local language models and embeddings.
- **[Claude Code CLI](https://claude.com/claude-code) (optional)** for cloud-grade reasoning and
  the coding agent.
- A microphone if you want voice. A GPU is not required.

## Install (from source)

```powershell
git clone https://github.com/Crackxsy/nox.git
cd nox

python -m pip install uv
python -m uv sync --extra dev --extra shell --extra voice   # add --extra rl for Rocket League

cd ui\pet       ; npm ci ; npm run build ; cd ..\..
cd ui\dashboard ; npm ci ; npm run build ; cd ..\..

.venv\Scripts\python.exe -m nox.cli onboard     # name, language, folders, integrations, consent
.venv\Scripts\python.exe -m nox.cli doctor      # honest environment report - read this
.venv\Scripts\python.exe -m nox.cli supervisor  # normal start: supervisor spawns core and shell
```

Other entry points: `nox dev` (core + shell in one console, no supervisor), `nox core --no-voice`,
`nox shell`, `nox rl calibrate`, `python -m nox.worker --service voice --selftest` (devices,
models, TTS, wake-word gate, a 3-second microphone check) and
`python -m nox.worker --download-kokoro` (Kokoro TTS model files).

Runtime files live in `%APPDATA%\Nox\runtime\{session.token,ipc.json,supervisor.token}`, logs in
`%APPDATA%\Nox\logs`. Your data and vault folders are chosen during onboarding and are never inside
the program folder. Open the dashboard from the tray menu — the access token travels in the URL
fragment, never in a query string. Kill switch: `ctrl+alt+shift+k`, the tray, the dashboard, the
pet's own menu, or the spoken phrase "Nox, Notaus".

## Setting up the integrations

Every integration is optional and off until you configure it. Secrets always go into the **Windows
Credential Manager** — never into a file in this repository, never into a log, never into a prompt.
`nox onboard` walks through the same steps interactively; the commands below are the manual route.

### Twitch (chat bot)

The dashboard route is the recommended one:

1. Open the dashboard → **Einstellungen → Twitch**.
2. Register an application at <https://dev.twitch.tv/console/apps> → **Register Your Application**.
   Category **Chat Bot**, Client Type **Public**, OAuth redirect URL `http://localhost` (unused by
   the device flow, but the form requires one).
3. Copy the **Client-ID** into the dashboard field and click **"Mit Twitch verbinden"**.
4. Nox shows a short code; open <https://www.twitch.tv/activate>, enter it, and approve. The token
   lands in the Credential Manager on its own.

> The device-code flow is being implemented right now. Until it ships, use the manual fallback:
> generate a chat OAuth token for your bot account, then
> ```powershell
> .venv\Scripts\python.exe -m nox.cli secrets set nox/twitch/oauth_token
> .venv\Scripts\python.exe -m nox.cli secrets set nox/twitch/bot_username
> ```
> Both commands read the value from a hidden prompt — never from the command line, so it does not
> end up in your shell history.

The Twitch plugin only loads in the **stream** profile, and its egress allowlist is limited to
`irc.chat.twitch.tv:6697` and `api.twitch.tv:443`.

### OBS Studio (scene control)

1. In OBS: **Tools → WebSocket Server Settings** → enable the server, set a password.
2. Store it: `.venv\Scripts\python.exe -m nox.cli secrets set nox/obs/websocket_password`
   (or paste it in the dashboard under **Einstellungen → OBS**).

Nox talks obs-websocket v5 over loopback only. Available tools: read-only status and scene
inventory, one *confirmed* scene switch, and a privacy-scene safety action. Deleting scenes or
sources and stopping the stream are not implemented — deliberately.

### Telegram (mobile bridge)

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. `.venv\Scripts\python.exe -m nox.cli secrets set nox/telegram/bot_token`

### Claude Code (cloud reasoning, coding agent)

Install the [Claude Code CLI](https://claude.com/claude-code) and run `claude login` once. Nox
shells out to the CLI and uses its login — it never stores an API key of its own. If you are not
logged in, `nox doctor` says so and the router falls back to Ollama.

### Ollama (local models)

```powershell
ollama pull llama3.2:3b        # chat / reasoning
ollama pull nomic-embed-text   # embeddings for memory search
```

Ollama runs on `127.0.0.1:11434`; nothing leaves the machine. It is also the fallback whenever the
cloud provider is unavailable, and the only allowed provider in the `offline` and `work` profiles.

### Voice (microphone and speech)

Voice needs the `voice` extra and model files. Nothing is ever downloaded on its own: a missing
model is an `unavailable` health reason that names the path and the command that fetches it. All
voice models live under `<data_dir>/models/` (`piper/`, `kokoro/`, `openwakeword/`); if your
`paths.data_dir` is not the default, set `voice.models_dir` in your user layer as well — the voice
worker only ever receives the `voice` section of the configuration.

**Text-to-speech engines.** `voice.tts.engine` is `piper` (default) or `kokoro`:

| | `piper` | `kokoro` |
| --- | --- | --- |
| German voice | yes (`de_DE-thorsten-medium`) | **no** — Kokoro v1.0 has no German voice; German text is read by the English `af_heart` voice, and health reports `limited` |
| Latency (7 s of speech, CPU) | ~90 ms to first audio, RTF 0.03 | ~750-840 ms to first audio, RTF 0.22 |
| Extra | `voice` | `voice-kokoro` + `python -m nox.worker --download-kokoro` (~354 MB) |
| License | `piper-tts` is GPL-3.0 | package and model are permissive, **but** `kokoro-onnx` still pulls GPL-3.0 `phonemizer`/espeak-ng |

Switch with `voice.tts.engine: kokoro` in `%APPDATA%\Nox\user.yaml` and restart. Kokoro was added
to get bundled builds out from under the GPL (see [`NOTICE`](NOTICE)); it does not get there on its
own, because its grapheme-to-phoneme front end is still GPL-3.0. Treat the license question as
open, not solved.

**Listening and the wake-word gate.** Continuous listening used to send every detected speech
segment to Whisper, so a video or a game in the background could push the queue to 20–40 seconds
and make Whisper "recognise" words in noise. A small always-on detector
([openWakeWord](https://github.com/dscripka/openWakeWord), Apache-2.0, ONNX) now sits in front of
it: while push-to-talk is not held and no conversation window is open, a segment reaches Whisper
only if the wake word fired within `voice.stt.wake_window_s` (default 8 s). Everything else is
dropped before transcription — a privacy property as much as a CPU one, because audio you did not
direct at Nox is never turned into text at all.

openWakeWord ships no pre-trained model for "Nox", and training one is not part of this repository.
Until a model file is placed in `<data_dir>/models/openwakeword/`, the gate falls back to matching
the wake word on the Whisper transcript — the previous behaviour — and reports `limited` with that
reason (`python -m nox.worker --selftest` prints it). The voice kill phrase ("Nox Notaus") has no
acoustic model either, so short segments (`voice.stt.kill_watchdog_max_ms`, default 2.5 s) are
still transcribed to keep it working; if such a transcript is not the kill phrase it is discarded
immediately and never becomes an event or reaches a language model.

`voice.stt.listening_mode` chooses between `continuous` (default, microphone open behind the gate)
and `ptt_only`, which never opens the microphone unless push-to-talk is held.

### Profiles

A profile decides what Nox may do, independently of what the model asks for. Switch it from the
tray or the dashboard.

| Profile | Purpose |
| --- | --- |
| `companion` | Default: desktop pet, voice, memory. |
| `stream` | Twitch/OBS plugins load **only here**; no screenshots to the cloud. |
| `coding` | Claude Code sessions, filesystem limited to the workspaces you configure. |
| `research` | Cloud and web access allowed. |
| `work` | No cloud, no memory writes; local models only. |
| `rocket_league` | Observation-only game coaching; no cloud. |
| `offline` | Nothing leaves the machine. |

Shipped profiles contain **placeholder** paths only (`%USERPROFILE%/Projects`). Your real
workspace and replay folders belong in your user layer (`%APPDATA%\Nox\user.yaml`), which the
onboarding wizard writes and which is never part of this repository.

## Privacy model

- **No telemetry.** Nox never contacts its developers. The only outbound connections are to the
  providers and integrations *you* configured, and every one of them is visible in the dashboard.
- **Raw audio is never persisted**, in any mode. Transcripts are local and expire (7 days by
  default); logs are PII-filtered before they are written.
- **Privacy zones** (banking, password managers, e-mail, private chats, Discord by default) turn
  capture, screenshots, clipboard reads and memory writes off while such a window is in front.
- **Four privacy modes** — FULL, BALANCED (default), PRIVATE, OFFLINE. Moving to a more private
  mode never asks; moving back to FULL always does.
- **The capture indicator cannot be hidden.** If the microphone, screen or camera is on, you see it.

Details: [`docs/PRIVACY.md`](docs/PRIVACY.md).

## Security model

- **Hard guarantees live below the language model** — kill switch, privacy modes, permissions and
  prohibitions are enforced in code, so no prompt and no plugin manifest can talk its way past them.
- **Games are observation-only.** No input synthesis, no process-memory access; a CI job fails the
  build if such an API ever appears in `src/` or `plugins/rl/`.
- **Secrets only in the Windows Credential Manager** — never in code, config, logs or prompts. CI
  scans the working tree *and* the full git history on every run.
- **All egress goes through a guard** with a per-profile, per-plugin allowlist; plugins run as
  separate, manifest-constrained worker processes.
- **Tamper-evident audit log** (hash-chained) for every security-relevant event; a kill triggered by
  the security layer itself needs your PIN to resume.

Details: [`docs/SECURITY.md`](docs/SECURITY.md). Reporting a vulnerability: [`SECURITY.md`](SECURITY.md).

## Architecture

```
                      ┌──────────────────────────────────────────────┐
                      │            Supervisor (watchdog)             │
                      │  kill-switch hotkey path, restarts, tokens   │
                      └───────────────┬──────────────┬───────────────┘
                                spawns│              │spawns
                      ┌───────────────▼──────────┐   │   ┌──────────────────────┐
                      │      Core process        │   └──►│  Desktop shell (Qt)  │
                      │                          │       │  pet window, tray,   │
                      │  ┌────────────────────┐  │       │  global hotkeys      │
   plugins (separate  │  │   Security layer   │  │       └──────────┬───────────┘
   worker processes)  │  │ permissions,       │  │                  │ serves
   ┌───────────────┐  │  │ privacy modes/     │  │       ┌──────────▼───────────┐
   │ twitch  obs   │  │  │ zones, kill switch,│  │       │  ui/pet  (React/TS)  │
   │ telegram  rl  │◄─┼─►│ audit, secrets,    │  │       │  ui/dashboard        │
   │ coding  clips │  │  │ egress guard       │  │       └──────────────────────┘
   └───────────────┘  │  └─────────┬──────────┘  │
        IPC (local    │            │ every call  │
        WebSocket,    │  ┌─────────▼──────────┐  │
        typed,        │  │  Event bus, state  │  │
        versioned,    │  │  manager, health   │  │
        per-client    │  └──┬──────┬───────┬──┘  │
        tokens)       │     │      │       │     │
                      │  ┌──▼───┐┌─▼────┐┌─▼───┐ │
                      │  │  AI  ││Voice ││Memo-│ │
                      │  │router││worker││ ry  │ │
                      │  └──┬───┘└──────┘└──┬──┘ │
                      └─────┼───────────────┼────┘
                            │               │
               ┌────────────▼───┐    ┌──────▼─────────────┐
               │ Claude Code CLI│    │ SQLite + sqlite-vec│
               │ Ollama (local) │    │ vault (plain files)│
               │ rules fallback │    └────────────────────┘
               └────────────────┘
```

Every arrow crossing the security layer is checked: the permission engine decides
allow / confirm / deny per tool call, the egress guard decides whether a connection may be opened
at all, and the audit log records the decision.

## Repository layout

```
src/nox/core        lifecycle, config layering, logging, event bus, state manager, health
src/nox/security    permissions, profiles, privacy state, kill switch, audit, secrets, egress
src/nox/ipc         local WebSocket protocol (typed, versioned, authenticated), server, client
src/nox/ai          provider abstraction (Claude Code, Ollama, rules), router, fallback chain
src/nox/voice       STT/TTS abstractions and local engines, voice pipeline
src/nox/memory      SQLite (+ sqlite-vec), vault integration, sessions
src/nox/pet         pet state model (functional + mood)
src/nox/supervisor  independent watchdog / kill-switch path
src/nox/shell       PySide6 desktop shell (pet window, tray, hotkeys, dialogs)
src/nox/worker      worker runtime for services and plugins
src/nox/plugins     plugin manager and manifest validation
plugins/            shipped plugins, each with a manifest declaring tools, secrets and egress
ui/pet              React + TypeScript pet renderer (Vite)
ui/dashboard        React + TypeScript dashboard (Vite)
ui/shared/generated TypeScript types generated from the pydantic contracts
config/             defaults.yaml and profiles/*.yaml - no secrets, no machine-specific paths
scripts/            type generation, license audit, secrets scan, link check, repo setup
installer/          Inno Setup packaging
spikes/             measurement and feasibility scripts (not part of the product)
tests/              unit, integration and end-to-end tests
docs/               user guide, privacy, security, plugin authoring, release process
```

## Development

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m ruff format --check src tests
.venv\Scripts\python.exe -m mypy src/nox
.venv\Scripts\python.exe -m pytest tests/unit -m "not hardware and not network and not spike" -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests/integration -p no:cacheprovider
.venv\Scripts\python.exe scripts/gen_ts_types.py --check   # TS types match the pydantic contracts
.venv\Scripts\python.exe scripts/check_links.py            # every relative doc link resolves
cd ui\pet && npm test ; cd ..\dashboard && npm test
```

The pydantic models in `src/nox/ipc/protocol.py` and `src/nox/core/events.py` are the source of
truth for the TypeScript types under `ui/shared/generated/` — regenerate with
`python scripts/gen_ts_types.py` after changing a contract, or CI will fail.

CI (`.github/workflows/ci.yml`) runs all of the above on Windows runners, plus a Playwright
end-to-end smoke test, a guard against input-synthesis/process-memory APIs, a third-party license
audit and a full-history secrets scan.

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first — especially the security rules a pull request must
respect. Engineering conventions: [`docs/ENGINEERING.md`](docs/ENGINEERING.md). Writing a plugin:
[`docs/PLUGIN_AUTHORING.md`](docs/PLUGIN_AUTHORING.md). Participation is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Do not report vulnerabilities in a public issue. Use GitHub's private vulnerability reporting —
the process, timelines and scope are in [`SECURITY.md`](SECURITY.md).

## Roadmap

Short version, in order: finish the Twitch device-code flow and the integration settings UI →
Rocket League coaching quality (Stage 2 vision) → project-manager and research assistant → signed
installer with proven update/rollback → external security review → v1.0. The v1.0 gate itself is
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md).

## Support

GitHub Issues is the support channel, with no guaranteed response time and no paid tier — this is a
small project. Security reports get priority. Start with
[`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) §6 (troubleshooting) and `nox doctor`.

## License

Nox is licensed under the **Apache License 2.0** — see [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE).

- You may use, modify, distribute and sell it, privately or commercially, with no royalty.
- You must keep the license and copyright notices, state what you changed, and pass on the
  `NOTICE` file; the Apache-2.0 patent grant comes with it.
- It is provided **as is**, without warranty, and the optional `voice` extra pulls in
  GPL-3.0-licensed components — a build that bundles it (for example the Windows installer) is
  distributed under GPL-3.0 terms as a combined work. See [`NOTICE`](NOTICE) and
  [`docs/THIRD_PARTY_LICENSES.md`](docs/THIRD_PARTY_LICENSES.md).
