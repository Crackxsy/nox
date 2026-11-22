# Nox

[![CI](https://github.com/Crackxsy/nox/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Crackxsy/nox/actions/workflows/ci.yml)
[![Status: pre-1.0](https://img.shields.io/badge/status-pre--1.0-orange)](CHANGELOG.md)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Platform: Windows desktop, Linux server](https://img.shields.io/badge/platform-Windows%20desktop%20%7C%20Linux%20server-0078d4)](#platforms)
[![Python 3.13](https://img.shields.io/badge/python-3.13-3776ab)](#requirements)

A local-first AI companion — on your Windows desktop, and at home as a small server that your other
devices share — one entity with a stable personality, not a chat window with a mascot on top.

> **Kurzfassung (Deutsch):** Nox ist ein lokaler KI-Begleiter: auf dem Windows-PC ein Desktop-Pet
> mit Stimme, ein Twitch-/OBS-Streambot, ein Rocket-League-Coach (nur Beobachtung, niemals
> Eingaben ins Spiel) und ein Coding-Agent. Der Kern läuft auch als Heimserver unter Linux
> (Raspberry Pi); weitere Computer binden sich als Nodes an. Alles läuft so weit wie möglich auf
> dem eigenen Rechner, es gibt keine Telemetrie, und Kill-Switch, Privacy-Modi und Berechtigungen
> sind **unterhalb** des Sprachmodells in Code durchgesetzt. Die App spricht Deutsch oder
> Englisch; die Dokumentation ist englisch, drei Anleitungen gibt es auch auf Deutsch
> ([Sprachen](#languages)). Nox ist vor Version 1.0 — die Liste unten sagt ehrlich, was heute
> funktioniert.

## What Nox is

- **Desktop pet** — a small always-on-top window with a mood, a personality, and a local voice
  (speech-to-text and text-to-speech run on your machine).
- **Twitch / OBS stream companion** — chat responder, a "Funken" viewer loyalty currency, and a
  deliberately narrow OBS tool surface (scene switching is a confirmed action; there is no
  scene-delete and no stream-stop tool at all).
- **Rocket League coach** — **observation only**: it notices the game, reads the HUD from the
  screen and parses your replay files; it has no game-audio capture. Nox never sends input to the
  game and never reads or writes its process memory. This is a
  code-level boundary enforced by a CI job, not a setting you could flip.
- **Coding agent** — orchestrates the Claude Code CLI inside workspaces you configured, under the
  permission engine.
- **A personal computer agent** — it can look at and change files in folders you name, see what is
  running, close a window, draw you a chart, type into a window, and write a proposed change to its
  own source on a branch for you to read. Every one of those goes through the same permission check
  and audit entry as a click in the dashboard, and the things it must never do are refused in code
  rather than by a risk level.
- **Project/research/creative assistant** — partly built, see the honest status list below.

Design principles: one coherent entity rather than a chatbot with a pet skin; local first with no
hard cloud dependency; security before autonomy; no fake capabilities (every subsystem reports
`available` / `limited` / `unavailable` with a real reason); recoverable by design (supervisor,
degraded modes, rollback).

**New here? [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md)** is the four-step version:
install, `nox doctor`, `nox onboard`, `nox supervisor`.

## What works today (60-second version)

Nox is **pre-1.0**. Honest state of the `develop` branch (the latest release is 0.5.0; see
[`CHANGELOG.md`](CHANGELOG.md) for what is only in `develop` so far):

| Area | State |
| --- | --- |
| Core, supervisor, kill switch, privacy modes/zones, permission engine, audit chain | Works |
| Local IPC hub, desktop shell (pet window, tray, hotkeys), dashboard | Works |
| Voice: push-to-talk, hands-free listening with the wake word in the transcript (no trained "Nox" model yet), local STT (faster-whisper) + TTS (Supertonic 3 by default, Piper as the fallback, Kokoro) | Works, needs the `voice` extras and a model download (`nox voice setup`; the voice files also with one click in the dashboard); see [Voice](#voice-microphone-and-speech) |
| AI routing: Claude Code CLI → Ollama → deterministic rules fallback | Works; each provider reports its real health |
| Memory: SQLite + `sqlite-vec`, vault indexing | Works; first start indexes the whole vault once |
| Twitch chat bot, OBS scene control, Funken ledger, dashboard Stream page | Works (stream and rocket_league profiles) |
| Telegram bridge | Works once a bot token is set |
| Smart home via Home Assistant: lights, sockets, scenes, media, thermostats, blinds, dashboard Zuhause page | Works against the documented API; never tested against a real instance yet — see [Home Assistant](#home-assistant-smart-home) |
| Rocket League coach | Stage 1 works: game detection, HUD capture and calibration, replay parsing, short callouts. A first on-screen analysis (classical OpenCV colour heuristics) exists but is off by default and its thresholds are not calibrated on real matches; coaching quality is early |
| Tools in a conversation: anything on the dashboard is reachable by typing or saying it | Works; the model is offered only what the active profile allows |
| Capability catalogue: what Nox can do, may do, and cannot do, as data | Works; "I have no entry for that" is a real answer |
| Presets: one phrase, several systems | Works; ships empty, so a fresh install can run nothing |
| Plans: several steps, approved once, surviving a restart | Works; an interrupted step is reported, never repeated |
| Files in the folders you name | Works; `files.roots` ships empty, and deleting only ever means the Recycle Bin |
| Windows and processes: see them, focus, close, end | Works; Windows' own processes, Nox and a running game are refused in code |
| Board: Nox draws tables, charts and facts in the dashboard | Works; it sends values, never markup |
| Typing into a window | Works; refuses entirely while a watched game runs anywhere |
| Self-extension: Nox proposes a change to its own source on a branch | Mechanism works and is tested; **not proven end to end** - needs the coding profile and a configured workspace |
| Coding agent (Claude Code orchestration) | Works inside configured workspaces; still rough |
| Clips, creative-app detection, mobile companion | Early, partly scaffolding |
| Linux server / Raspberry Pi: installer, update, rollback, snapshots | Built and exercised in CI on Linux runners; not part of a published release yet (the latest, 0.5.0, predates it), so the one-line command in [`docs/RASPBERRY_PI.md`](docs/RASPBERRY_PI.md) only works once a release carries the script |
| Nodes: Nox's hands on your other computers (Windows, Linux; macOS beta) | Works; see [`docs/NODES.md`](docs/NODES.md) for what a node offers and the known limits |
| Nox-OS: a flashable Raspberry Pi image | Built and boot-tested in CI; not a release asset yet, and the guided setup after the first claim is not built ([`os/README.md`](os/README.md)) |
| Windows installer | Builds, and CI installs, upgrades and uninstalls it on a clean runner; **unsigned**, never tried by hand on a clean machine, no download yet - run from source for now |
| Signed releases, auto-update (Windows) | The signing mechanism is built, the release key is not set up, so nothing is verified yet ([`docs/RELEASES.md`](docs/RELEASES.md)); the Windows desktop has no auto-update |

`nox doctor` prints the same picture for *your* machine, including why something is unavailable.
See [`CHANGELOG.md`](CHANGELOG.md) for what shipped when.

## Platforms

What runs where, as shipped today:

| Platform | Runs | How you get it today | Not available there |
| --- | --- | --- | --- |
| **Windows 11** | The full desktop companion: core, pet window, tray, hotkeys, voice, window and program control, privacy zones, Twitch/OBS and the Rocket League coach. Also a node. | From source ([`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md)); an unsigned installer you can build yourself ([`installer/README.md`](installer/README.md)) | - |
| **Linux server**: Raspberry Pi 4/5 (64-bit Raspberry Pi OS), Debian 12+, Ubuntu 22.04+, ARM64 or x86-64 | The core as a service with the dashboard, for the whole household ([`docs/RASPBERRY_PI.md`](docs/RASPBERRY_PI.md)). Also a headless node. | The installer script, which is in this repository but not yet in a published release (see the table above) | The pet window, hotkeys, window and program control, Twitch/OBS and the Rocket League coach (these belong on the PC); voice on the Pi is planned |
| **Linux desktop** | A node: Nox's hands on that computer ([`docs/NODES.md`](docs/NODES.md)) | The same installer script, `--node` | The core with the desktop shell |
| **macOS** | A node, **beta**: tested less than the others, CI lints the installer and runs the node's tests | `scripts/install-macos.sh --node` from a checkout; it does not verify release signatures | The core, the desktop shell, shell commands and media control on the node |
| **Phone, tablet, any browser in the home network** | The dashboard, once the device is paired ([`docs/NETWORK_ACCESS.md`](docs/NETWORK_ACCESS.md)) | A QR code; no app to install | Changing how Nox is set up |

The Windows-only parts are the ones that need the Windows desktop: the pet window, global hotkeys,
per-window privacy zones, typing and window control, the AppContainer command sandbox. CI runs the
tests on Windows, on Linux (x86-64 and ARM64) and, for the node, on macOS.

## Requirements

- **For the Windows desktop companion:**
  - **Windows 11.**
  - **Python 3.13 from [python.org](https://www.python.org/downloads/)** — *not* the Microsoft
    Store build: Store Python virtualises `%APPDATA%`, so Nox's runtime token files end up
    somewhere other tools cannot read and startup fails with `token_acl_failed`.
  - **Node.js 20 or newer** to build the two UIs (the core serves the built bundles).
- **For the Linux server or a node:** see the guides in the table above; Python and the built
  dashboard come with the installer.
- **A way for Nox to think** — without one, Nox gives only canned rule answers (it greets you, tells
  the time, says who it is) and says so:
  - **[Claude Code CLI](https://claude.com/claude-code) (optional)** for cloud-grade reasoning and
    the coding agent; it needs a Claude account you sign in with.
  - **[Ollama](https://ollama.com) (optional)** for fully local language models and embeddings.
- A microphone if you want voice. A GPU is not required.

## Install (from source)

```powershell
git clone https://github.com/Crackxsy/nox.git
cd nox

python -m pip install uv
python -m uv sync --extra dev --extra shell --extra voice --extra voice-supertonic   # add --extra rl for Rocket League

cd ui\pet       ; npm ci ; npm run build ; cd ..\..
cd ui\dashboard ; npm ci ; npm run build ; cd ..\..

.venv\Scripts\python.exe -m nox.cli onboard     # name, language, folders, integrations, consent
.venv\Scripts\python.exe -m nox.cli doctor      # honest environment report - read this
.venv\Scripts\python.exe -m nox.cli supervisor  # normal start: supervisor spawns core and shell
```

Other entry points: `nox dev` (core + shell in one console, no supervisor), `nox core --no-voice`,
`nox shell`, `nox rl calibrate`, `python -m nox.worker --service voice --selftest` (devices,
models, TTS, wake-word gate, a 3-second microphone check) and `nox voice setup` (downloads the
configured TTS engine's model files with progress, after asking).

Runtime files live in `%APPDATA%\Nox\runtime\{session.token,ipc.json,supervisor.token}`, logs in
`%APPDATA%\Nox\logs`. Your data and vault folders are chosen during onboarding and are never inside
the program folder. Open the dashboard from the tray menu — the access token travels in the URL
fragment, never in a query string. Kill switch: `ctrl+alt+shift+k`, the tray, the dashboard, the
pet's own menu, or the spoken phrase "Nox, Notaus".

## Setting up the integrations

Every integration is optional and off until you configure it. Secrets go into the **Windows
Credential Manager** (on a Linux server without a keyring, into an owner-only `secrets.json` in
Nox's data folder) — never into a file in this repository, never into a log, never into a prompt.
`nox onboard` walks through the same steps interactively; the commands below are the manual route.

### Twitch (chat bot)

The dashboard route is the recommended one:

1. Open the dashboard → **Einstellungen → Twitch**.
2. Register an application at <https://dev.twitch.tv/console/apps> → **Register Your Application**.
   Category **Chat Bot**, Client Type **Public**, OAuth redirect URL `http://localhost` (unused by
   the device flow, but the form requires one).
3. Copy the **Client-ID** into the dashboard field and click **"Mit Twitch verbinden"**.
4. Nox shows a short code; open <https://www.twitch.tv/activate>, enter it, and approve. The token
   lands in the Credential Manager on its own, and the dashboard refreshes to "connected" once the
   background poll picks it up.

This is an OAuth 2.0 Device Code Grant against a public client (`chat:read chat:edit` only); Nox
never sees your password, and the token is refreshed in the background so a long stream does not
die of an expired token. If you prefer not to use the dashboard, the manual fallback still works:
generate a chat OAuth token for your bot account, then
```powershell
.venv\Scripts\python.exe -m nox.cli secrets set nox/twitch/oauth_token
.venv\Scripts\python.exe -m nox.cli secrets set nox/twitch/bot_username
```
Both commands read the value from a hidden prompt — never from the command line, so it does not
end up in your shell history.

The Twitch plugin only loads in the **stream** and **rocket_league** profiles, and its egress
allowlist is limited to `irc.chat.twitch.tv:6697`. The Twitch login endpoint (`id.twitch.tv`) is
not reachable from `rocket_league`, so renewing the token waits until you leave the game.

### OBS Studio (scene control)

1. In OBS: **Tools → WebSocket Server Settings** → enable the server, set a password.
2. Store it: `.venv\Scripts\python.exe -m nox.cli secrets set nox/obs/websocket_password`
   (or paste it in the dashboard under **Einstellungen → OBS**).

Nox talks obs-websocket v5 over loopback only. Available tools: read-only status and scene
inventory, one *confirmed* scene switch, and a privacy-scene safety action. Deleting scenes or
sources and stopping the stream are not implemented — deliberately.

### Home Assistant (smart home)

Nox controls your home through **your own** [Home Assistant](https://www.home-assistant.io/)
instance — the one integration that fits this project: it runs on your network, speaks one API for
hundreds of device brands, and needs no manufacturer account. Nothing about your home ever reaches
a vendor cloud; every request goes to the address you configure and nowhere else.

1. In Home Assistant: click your name (bottom left) → **Security** → **Long-lived access tokens** →
   **Create token**. Copy the value once — it is never shown again.
2. Store it: `.venv\Scripts\python.exe -m nox.cli secrets set nox/home/access_token`
   (or paste it in the dashboard under **Einstellungen → Zuhause**).
3. Set the address under **Einstellungen → Zuhause** (`home.host` / `home.port`; the default is
   `127.0.0.1:8123`, for a Home Assistant running on this machine) and press **Verbindung testen**.
   The button reports the real result: connected, wrong token, nothing listening, or blocked by the
   current privacy mode.
4. Enable the plugin: add `home` to `plugins.enabled` (dashboard: **Einstellungen → Plugins**).

**A Home Assistant somewhere else on the network** needs two allow-list entries, because Nox's
network is default-deny and a plugin may only reach what its manifest declares:

* `plugins/home/manifest.yaml` → `network.egress`, and
* `config/profiles/companion.yaml` → `egress_allowlist`.

The shipped manifest already covers `127.0.0.1:8123` and `homeassistant.local:8123`. For a fixed
address add `192.168.x.y:8123` to both. A missing entry is a clear startup failure with the reason,
never a silent one.

**What Nox can do**: list areas, devices and their state; switch lights (on/off, brightness, colour
temperature), sockets and scenes; play/pause/skip/volume on media players; set a thermostat's
target temperature; open and close blinds. Scripts and automations can be triggered and always ask
first.

**What Nox can never do**: Home Assistant's `lock`, `alarm_control_panel` and `valve` domains, and
any `cover` that reports itself as a garage, a gate or a door, have no tool at all. They are
filtered out of every listing and refused by name with a logged reason. This is a hard boundary in
code (`src/nox/home/boundary.py`), not a permission setting — a language model must not be able to
unlock a door, and "it would ask first" is not the same guarantee.

Spoken and typed commands are matched **locally** first: "mach das Licht im Wohnzimmer aus" is
resolved against your own Home Assistant names in well under a millisecond, with no language model
involved. Only a sentence that does not match is handed to the model.

### Calendar and reminders (your day)

Asked "was steht heute an?", Nox answers from **your** calendars and reminders - not from its own
project backlog, which is a separate list it also keeps. Calendars are read as private iCal feeds;
nothing is written back, and no Google or school account is ever handed to Nox.

1. **Google Calendar**: open it in the browser → **Settings** → pick the calendar on the left →
   **Integrate calendar** → copy the **Secret address in iCal format**.
2. **School timetable**: in WebUntis, **Profile → Sharing** → copy the ICS link (other timetable
   systems that export an `.ics` URL work the same way).
3. Paste each address in the dashboard under **Einstellungen → Integrationen → Kalender**, or
   store it with `.venv\Scripts\python.exe -m nox.cli secrets set nox/calendar/google_ics_url`
   (and `nox/calendar/school_ics_url`). The address *is* the credential - anyone who has it can
   read the whole calendar - so it lives in the secret store (the Windows Credential Manager) like a token and is
   never written to a file, a log or a prompt. A failed fetch is reported as its HTTP status, not
   its URL.

Feeds are fetched every 15 minutes (`day.refresh_minutes`) for the next two weeks
(`day.lookahead_days`); a question refreshes early when the cache is stale, but never waits more
than a few seconds for the network. The hosts `calendar.google.com` and `*.webuntis.com` are on
the `companion` and `stream` profiles' egress allow-lists; another provider's host goes into
`config/profiles/companion.yaml` → `egress_allowlist` like any other.

**Reminders** need no setup. "Erinner mich morgen um 9 an den Zahnarzt" is stored in the database
(`reminder.set`), survives a restart and is spoken when due - once, as a toast too, respecting
mute and quiet zones but not the proactive budget, because you asked for exactly this at exactly
this time. The **Zuhause** page opens with **Dein Tag**: today's appointments, the reminders
(spoken ones greyed out), the next few after today, and a field to add one by hand.

### Telegram (mobile bridge)

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. `.venv\Scripts\python.exe -m nox.cli secrets set nox/telegram/bot_token`

### Claude Code (cloud reasoning, coding agent)

Install the [Claude Code CLI](https://claude.com/claude-code) and run `claude login` once. Nox
shells out to the CLI and uses its login — it never stores an API key of its own. If you are not
logged in, `nox doctor` says so and the router falls back to Ollama.

When your plan's usage limit (the five-hour or weekly window) is used up, Nox says so once, in plain
words and with the time it resets (spoken only where speaking is allowed: not muted, not in a
privacy zone, quiet hours, focus mode or while a game runs - it is always shown as a notification),
keeps Claude out of the chain until then and answers with the local model (or, without one, with
the fixed offline answers). Health is read from the answers Nox
really gets plus two local checks (`claude --version`, `claude auth status`), so asking "is Claude
there?" costs no quota. `ai.providers.claude_code.health_roundtrip: true` restores the old probe
that sends a real 1-token request with every health check.

### Ollama (local models)

```powershell
ollama pull qwen3:4b-instruct   # chat / reasoning (the default model)
ollama pull nomic-embed-text   # embeddings for memory search
```

Ollama runs on `127.0.0.1:11434`; nothing leaves the machine. It is also the fallback whenever the
cloud provider is unavailable, and the only allowed provider in the `offline` and `work` profiles.

### Voice (microphone and speech)

Voice needs the `voice` extra and model files. Nothing is downloaded without your go-ahead: a
missing model is an `unavailable` health reason that names the path and the ways to fetch it. All
voice models live under `<data_dir>/models/` (`piper/`, `kokoro/`, `supertonic/`,
`openwakeword/`); if your
`paths.data_dir` is not the default, set `voice.models_dir` in your user layer as well — the voice
worker only ever receives the `voice` section of the configuration.

**Text-to-speech engines.** `voice.tts.engine` is `supertonic` (default), `piper` (the fallback)
or `kokoro`:

| | `supertonic` (default) | `piper` (fallback) | `kokoro` |
| --- | --- | --- | --- |
| German voice | yes — ten preset voices `F1`–`F5` / `M1`–`M5` (default `F3`); the most natural of the three | yes (`de_DE-thorsten-medium`) | **no** — Kokoro v1.0 has no German voice; German text is read by the English `af_heart` voice, and health reports `limited` |
| Latency (CPU) | ~0.4-0.6 s per short sentence (4 threads, 5 steps), RTF ~0.13-0.19; no GPU needed | ~50-90 ms to first audio, RTF 0.02-0.03 | ~750-840 ms to first audio, RTF 0.22 |
| Extra + model | `voice-supertonic`; model files ~400 MB, downloaded after you agree (dashboard, onboarding or `nox voice setup`) | `voice` | `voice-kokoro` + `nox voice setup` with `engine: kokoro` (~354 MB) |
| License | package MIT, no phonemizer (no GPL); **model weights are OpenRAIL-M** — free, commercial use allowed, but with use restrictions (disclose that the voice is AI-generated, no impersonation, no medical advice); see [`NOTICE`](NOTICE) | `piper-tts` is GPL-3.0 | package and model are permissive, **but** `kokoro-onnx` still pulls GPL-3.0 `phonemizer`/espeak-ng |

**Getting the Supertonic model.** The model files are not part of the install. Fetch them with one
click under *Settings → Voice* in the dashboard (it shows what is missing, the size, the license,
and a progress bar), in the optional step of `nox onboard`, or with `nox voice setup` (asks first;
`--yes` skips the question). None of them downloads anything in the privacy modes `offline` or
`private`. The voice worker loads its engine at start, so the new voice is used from Nox's next
start. Until then - and whenever Supertonic cannot load (files missing, extra not installed,
unknown voice) - Nox speaks with Piper, and the voice health shows `limited` with the reason.
`voice.tts.voice` picks one of `F1`–`F5` / `M1`–`M5` (empty = `F3`; the dashboard lists the
installed ones), `voice.tts.steps` (1-32, default 5) trades speed for a slightly more natural
sound. The explicit `nox voice download-supertonic` / `python -m nox.worker --download-supertonic`
still work.

**Disclosing the AI voice on stream.** The Supertonic license requires disclosing machine-generated
audio, so Nox does it by default (`stream.ai_voice_disclosure`): when a stream session starts it
posts one line to the Twitch chat - "Kurzer Hinweis: Die Stimme von Nox ist KI-generiert." (or the
English equivalent, by `identity.speech_language`; `chat_text` replaces it) - once per session,
not again on a reconnect, and the OBS overlay shows a small, semi-transparent "KI-Stimme" label in
its corner (`overlay_label`; empty = no label). The desktop pet does not show the label. Both are
editable under *Settings → Integrations* and apply at the next start.

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

`voice.stt.listening_mode` chooses between `ptt_only` (the default: the microphone stays closed
until you hold push-to-talk) and `continuous`, which keeps it open behind the wake-word gate for
hands-free use. Change it in the dashboard under Einstellungen.

**Telling speech from other sound.** What decides that "someone is talking" used to be loudness
alone, so music, a game or a television opened a segment just as a voice did. The default is now
the [Silero VAD](https://github.com/snakers4/silero-vad) network (MIT, ONNX Runtime, about 1.2 MB):
it says whether a 32 ms block *sounds like speech*, with a start threshold of 0.5 and an end
threshold of 0.35 so one sentence is not cut in pieces (`voice.vad`). On synthetic test signals the
energy detector called 97-100 % of the frames of music and game sound "speech" and Silero 0 %,
while it still hears speech at -45 dBFS; one block costs about 0.1 ms on one CPU thread against a
budget of 40 ms. The model is not downloaded: Nox uses the copy inside the `faster-whisper`
package, and only loads a file whose SHA-256 it knows. If the model, `onnxruntime` or the time
budget fails, Nox switches to the energy detector by itself and the voice health line says so
("speech detection: energy fallback (...)"). `voice.vad.engine: energy` chooses it deliberately.
This detector only decides where an utterance starts and ends; whether it is addressed to Nox is
still the wake-word gate's job.

**Where the words go.** Silero decides *where* you speak; the sentence ends after `voice.vad.end_silence_ms`
(default 400 ms, was 600). What comes after "Nox" reaches Nox as Whisper wrote it - capital letters,
commas, "18:30", "3,5" - not lower-cased and stripped (names and numbers used to be mangled before
the memory or the language model saw them).

**The speech model never downloads itself.** `faster-whisper` used to fetch the model from Hugging
Face on first use, without asking, without a pin, also in the privacy modes `offline` and
`private`. Now the engine only reads from disk. `nox voice setup` fetches it - pinned to one
commit of the model repository, only after a yes, and never in `offline`/`private` - and a missing
model is reported with that command. Models of an older install (faster-whisper's own cache layout
under the models folder) keep working. `base`, `small` and `tiny` have pins; for `medium` and
`large-v3-turbo` place the files in `<models_dir>/faster-whisper/whisper-<size>/` yourself. The
dashboard tile and the onboarding step still cover the voice (TTS) files only.

**Quiet next to a game.** The voice worker runs at normal priority (it used to run above normal)
and at `below_normal` while a game is in the foreground (`voice.performance.priority` and
`game_priority`, Windows priority classes). Speech recognition uses at most 8 CPU threads, fewer
with `voice.performance.cpu_threads` (read when the model loads). The effect on frame rates is
not measured.

**Nox hearing itself.** Through loudspeakers the microphone picks up Nox's own voice, and a speech
detector cannot tell it from yours: Nox would interrupt itself. At start Nox looks at what it plays
on - Windows reads the endpoint's form factor (what the sound settings use for the device icon, read
only from the registry), Linux asks PulseAudio/PipeWire (`pactl`), macOS `system_profiler` (this one
is untested on real hardware), and a words-in-the-name guess backs each of them. Headphones, a
headset and stream-only or muted output change nothing. For loudspeakers and for an output it cannot
identify, speech has to be louder than `voice.echo.start_db` (default -30 dBFS) for
`voice.echo.min_ms` (240 ms, short dips inside a voice do not count against it) before it counts as
you, while Nox speaks and for `voice.echo.tail_ms` (400 ms) afterwards; the room's echo of a
normally played voice is quieter than a person at the microphone, so an interruption still works.
`voice.echo.mode: half_duplex` hears nothing while Nox speaks (push-to-talk still works), `off`
switches the rule off. This is a threshold, not echo cancellation: a very loud output or a
microphone next to the speaker defeats it, and speaking quietly over loud speakers is not heard.
Until real echo cancellation exists the voice health line says "headphones recommended" whenever
the output is not headphones; the device is looked at again when Nox starts speaking (at most every
30 s) and when the output device is changed in the settings.

**When the microphone goes away.** A headset that is unplugged, a driver reset or a sleep/wake cycle
used to leave Nox deaf without a sign. The capture loop now runs under a supervisor: if the input
fails or stops delivering audio while capture is meant to be on, it is closed and opened again after
1, 2, 5, 10 and then every 30 seconds (a microphone that keeps dropping out waits longer each time).
The voice health line shows `limited` with the reason ("microphone: ..."). After three failures in a
row the core says once, on the private channel, "Ich höre dich gerade nicht." ("I can't hear you
right now."), and once more when audio flows again - but only where unprompted speech is allowed
(not muted, no privacy zone, not in a quiet privacy mode, outside quiet hours); otherwise the health
line is all you get. A microphone that keeps dropping out is announced once as "wackelt". A
microphone that is closed on purpose (push-to-talk waiting, muted, privacy off) is silence, not a
failure, and a restart never opens a device the privacy gate keeps closed. The list of input
devices is compared every 2 seconds while the microphone is down, but PortAudio only knows the
devices that were present when audio was initialised: a headset plugged in later is reliably found
at the next restart of the voice service (re-initialising PortAudio would cut off speech that is
playing).

**How fast a spoken answer is.** The voice worker times every voice turn from the end of your
utterance to the first audio chunk handed to the output device: the end-of-speech decision, the
transcription (`stt_ms`), the core's wait for the first sentence (`reply_ms`, which contains the
model's first token) and the speech synthesis (`synth_ms`). Each turn is one structured log line
(`voice.turn_latency`, milliseconds only, never the words), and the request `voice.latency` returns
P50/P95 over the last 50 turns. Nothing leaves the machine. Time since your last word is
`total_ms` plus `endpoint_ms`, the silence the detector waits for (zero with push-to-talk).

### Profiles

A profile decides what Nox may do, independently of what the model asks for. Switch it from the
tray or the dashboard.

| Profile | Purpose |
| --- | --- |
| `companion` | Default: desktop pet, voice, memory. |
| `stream` | Twitch/OBS plugins load here (and in `rocket_league`); no screenshots to the cloud. |
| `coding` | Claude Code sessions, filesystem limited to the workspaces you configure. |
| `research` | Cloud and web access allowed. |
| `work` | No cloud, no memory writes; local models only. |
| `rocket_league` | Observation-only game coaching; no cloud. The one exception: the Twitch chat connection and the local OBS websocket, so the chat bot keeps answering during a game. |
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
- **Secrets only in the OS secret store** (the Windows Credential Manager; on a Linux server
  without a keyring an owner-only file) — never in code, config, logs or prompts. CI
  scans the working tree *and* the git history on every run - for a pull request every commit it
  adds, for a branch its whole history.
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

Beyond the one machine - the other devices of a household reach the core over two listeners of
their own, and Nox reaches *them* only through a node they started themselves:

```
  other devices in the home                    Core process (a PC, or a Raspberry Pi with Nox-OS)
 +-------------------------------+           +--------------------------------------------------+
 | phone, tablet, another PC:    |  HTTPS    | nox.lan     browser listener :47843 - own        |
 | the dashboard in a browser    +---------->|             certificate authority, paired        |
 +-------------------------------+  paired   |             devices only                         |
                                             |                                                  |
 +-------------------------------+           | nox.node    node listener :47844 - routes a tool |
 | nox-node on another computer: | mutual    |             call to the device it is meant for,  |
 | its own gate, its own audit   | TLS 1.3   |             signs it, cleans what comes back     |
 | trail, a question on the      +---------->|                                                  |
 | device for high risk          | the node  | nox.hands   shell, packages, web, browser, senses|
 +-------------------------------+ dials out | nox.desktop windows and processes, guarded input |
                                             | nox.noxos   only on the Nox-OS image             |
                                             +--------------------------------------------------+
```

A node never accepts a connection, and it checks every call again by its own rules - whatever
the core decided. The map through that code, with one call followed end to end:
[`docs/ARCHITECTURE-NODES.md`](docs/ARCHITECTURE-NODES.md).

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
src/nox/home        smart-home boundary, deterministic intent matching, `home.*` IPC, connection probe
src/nox/lan         network access: the installation's own certificate authority, paired devices, HTTPS listener
src/nox/node        nodes - Nox on your other computers: `hub/` (listener, routing, pairing) on the core, `device/` (`nox-node`) on the device, `wire/` (protocol, receipts) for both
src/nox/hands       the tools that reach the machine and the world: shell, packages, web, browser, senses
src/nox/desktop     processes and windows on this machine; typing and computer use behind the game guard
src/nox/sandbox     runs commands and foreign code confined (AppContainer on Windows, bwrap on Linux)
src/nox/noxos       what the core does only on the Nox-OS image: the claim, remote maintenance
src/nox/...         further packages (memory views, presets, skills, release, ...) - each `__init__.py` says what it owns
plugins/            shipped plugins, each with a manifest declaring tools, secrets and egress
ui/pet              React + TypeScript pet renderer (Vite)
ui/dashboard        React + TypeScript dashboard (Vite)
ui/shared/generated TypeScript types generated from the pydantic contracts
config/             defaults.yaml and profiles/*.yaml - no secrets, no machine-specific paths
scripts/            type generation, license audit, secrets scan, link check, repo setup
installer/          Inno Setup packaging, the node launcher, Linux service files
os/                 Nox-OS: the flashable Raspberry Pi image (pi-gen stages, provisioning, image checks)
evals/              capability benchmark: everyday requests against a headless core, scored automatically
spikes/             measurement and feasibility scripts (not part of the product)
tests/              unit, integration and end-to-end tests
docs/               user guide, presets, privacy, security, plugin authoring, release process
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

CI (`.github/workflows/ci.yml`) runs all of the above on Windows runners, the Python tests also on
Linux (x86-64 and ARM64) and the node's on macOS, plus a Playwright end-to-end smoke test, a guard against input-synthesis/process-memory APIs, a third-party license
audit and a secrets scan of the working tree and the branch's history.

## Languages

- **The app** speaks German or English. `identity.ui_language` (`de` by default, set at onboarding)
  chooses the language of the tray menu and dialogs, the terminal output, the onboarding wizard and
  the language Nox answers in by default. The dashboard and the pet window choose theirs
  separately, in the browser: a `?lang=de|en` in the address, else the browser's preferred
  language, else German - so an English browser shows them in English whatever `ui_language` says.
- **The documentation is English.** Three guides also exist in German: Nodes
  ([`docs/NODES.de.md`](docs/NODES.de.md)), Network access
  ([`docs/NETWORK_ACCESS.de.md`](docs/NETWORK_ACCESS.de.md)) and Raspberry Pi
  ([`docs/RASPBERRY_PI.de.md`](docs/RASPBERRY_PI.de.md)). **Where an English and a German version
  differ, the English one is authoritative**; the German ones are translations and can lag behind. Everything else - this README apart from its short German summary at the
  top, the user guide, security and privacy statements, the changelog - exists in English only.
- Code, comments, commit messages and pull requests are English too
  ([`CONTRIBUTING.md`](CONTRIBUTING.md)).

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first — especially the security rules a pull request must
respect. Engineering conventions: [`docs/ENGINEERING.md`](docs/ENGINEERING.md). Writing a plugin:
[`docs/PLUGIN_AUTHORING.md`](docs/PLUGIN_AUTHORING.md). Participation is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Do not report vulnerabilities in a public issue. Use GitHub's private vulnerability reporting —
the process, timelines and scope are in [`SECURITY.md`](SECURITY.md).

## Roadmap

Short version, in order: Rocket League coaching quality (Stage 2 vision) → project-manager and
research assistant → signed installer with proven update/rollback → external security review →
v1.0. The v1.0 gate itself is [`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md).

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
