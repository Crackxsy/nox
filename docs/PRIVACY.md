# Privacy statement

This document describes, in plain language, what Nox captures, what it stores, what ever leaves
your device, and how you control all of that. It is derived from and must stay consistent with the
project's internal Security Model §1 (threat model) and §5 (privacy) — if this file and the Security
Model ever disagree, the Security Model is authoritative and this file has a bug.

**No telemetry.** Nox sends nothing to its developer or anyone else automatically. The only
outbound network connections Nox ever makes are to providers and integrations *you* configured
(your AI provider, your Twitch/OBS/Telegram, your local Ollama) — every one of them is visible in
the dashboard.

## What is captured

Depending on what you enabled at onboarding (`nox onboard`) or later:

- **Microphone audio** — only while actively listening (push-to-talk held, or after the wake word
  fires), never persisted as raw audio (see "What is stored" below).
- **Screen / HUD** — only for features that need it (e.g. Rocket League observation), and never
  while a privacy zone is active (see below).
- **Camera** — off by default; only if you explicitly enable it.
- **Game state (Rocket League)** — screen/HUD capture, game audio, replay files, and
  non-invasive input *observation* (statistics from a global hotkey listener — Nox never sends
  input to the game). This is a hard, code-level boundary, not a setting: no file in the codebase
  may import an input-synthesis or process-memory API for a game window, and CI fails the build if
  one ever appears.
- **Twitch chat / stream events** — only if you connected Twitch at onboarding.

A **capture indicator** in the desktop shell always shows the true mic/screen/camera state. It
cannot be hidden or disabled by configuration.

## What is stored, and for how long

- **Transcripts** (from voice) — kept locally, subject to a retention window (7 days by default,
  configurable) — never uploaded anywhere except as part of a request to whichever AI provider you
  chose, under the rules below.
- **Logs** — local, JSON-structured, filtered for personally-identifying content and secrets
  before being written, rotated (14 days by default).
- **Memory / vault notes** — local files in your chosen vault folder, meant to be yours: readable,
  portable, and never written to while a privacy zone is active for the thing being discussed.
- **Raw audio is never persisted**, in any mode, at any privacy level. Only the transcript (if
  transcription is enabled) is kept, under the retention window above.
- **Secrets** (API keys, OAuth tokens, passwords for Twitch/OBS/Telegram) — the Windows Credential
  Manager only. Never a config file, never a log line, never included in a prompt sent to an AI
  provider.
- **Audit log** — a local, tamper-evident (hash-chained) record of security-relevant events
  (mode/privacy changes, kill switch, permission decisions, config changes, tool executions with
  side effects, memory deletions, plugin lifecycle). The audit log itself never contains secrets
  or raw private content.

## What ever leaves your device, and under what conditions

- **Nothing, unless you chose a cloud AI provider** (e.g. Claude Code). In that case, the portion
  of a conversation/request relevant to answering it is sent to that provider under its own terms
  — Nox does not add a second destination.
- **Cloud vision** (if/when a vision feature is enabled) only sends a screenshot to a cloud
  provider in **FULL** privacy mode, and never while a privacy zone is active for that window.
- **Local-only providers** (Ollama) never leave the machine, in any privacy mode.
- **Stream integrations** (Twitch IRC, OBS websocket) talk only to the endpoints you configured,
  over the connections you set up — never proxied through a third party Nox controls.
- In **PRIVATE** and **OFFLINE** modes, only an explicit allow-list of loopback (same-machine)
  services can be reached at all (Ollama by default, plus e.g. the OBS websocket in the stream
  profile) — everything else, including other loopback ports, is denied and logged in the audit
  trail.

### Voice: what reaches the speech recogniser

With `voice.stt.listening_mode: continuous`, microphone audio passes a local wake-word gate
first; audio that does not pass the gate is never transcribed. Short segments still reach the
recogniser for the voice kill-phrase watchdog, and those transcripts are discarded without
becoming an event. With `ptt_only`, the microphone is only open while push-to-talk is held.

## Privacy modes

| Mode | What changes |
|---|---|
| **FULL** | Cloud AI and cloud vision allowed (screenshots to cloud only outside privacy zones). |
| **BALANCED** (default) | Cloud AI allowed for conversation; more capture/egress constrained than FULL. |
| **PRIVATE** | Cloud providers denied; only allow-listed loopback services reachable. |
| **OFFLINE** | No network egress at all, including loopback services not on the allow-list. |

Switching to a *more* private mode (e.g. FULL → PRIVATE) is always immediate, no confirmation.
Switching back toward FULL always asks you to confirm first — Nox never silently becomes less
private.

## Privacy zones

You can mark apps/windows (by process name or title pattern) as zones Nox should never look at —
banking, password managers, email, private chats, personal documents, and Discord are zoned by
default. While a zone is the foreground window: no capture, no screenshot, no clipboard read, and
no memory write referencing it. Zone detection happens locally (it only looks at the foreground
window's title/process name) and that detection itself never leaves the machine.

## How to inspect or delete your data

- **Vault**: it is a folder of plain files you already own — open it in any editor, or delete
  individual notes yourself. Nox never deletes vault content silently.
- **Transcripts, logs, cache, database**: all under your configured data folder
  (`%APPDATA%\Nox` by default); delete the folder (while Nox is stopped) to reset to a clean
  state without touching the vault.
- **Secrets**: `nox secrets delete <name>`, or remove them directly from Windows Credential
  Manager.
- **Audit log**: inspectable from the dashboard; resetting it requires your PIN (it is a security
  control, not a convenience feature).

## Questions this document does not answer on its own

For *why* each of these controls exists and how it is technically enforced (not just described),
see `SECURITY.md` and the full Security Model. If anything in this statement seems to conflict
with Nox's actual behavior, please open a GitHub issue — see the support policy in `README.md`.

## The one host Nox is allowed to reach out of the box

The shipped network allow-list (`security.egress_allowlist` in `config/defaults.yaml`) contains a
single entry: `id.twitch.tv:443`, Twitch's OAuth endpoint. It is there so that connecting your
Twitch account is a button ("log in with Twitch") instead of a copy-pasted token, and it is only
ever contacted when *you* start or renew that login — Nox never talks to it on its own. It is
blocked in privacy mode PRIVATE and OFFLINE like every other non-local host, and the "offline",
"work" and "rocket_league" profiles do not include it at all. If you never connect Twitch, nothing
is ever sent there. Everything else Nox may reach comes from the profile you are in (e.g. the
Twitch chat server in the "stream" profile, the Telegram bot only once you enable the Mobile
Companion), and each of those is listed in that profile's file.

## Your personality file

Nox's character is a plain Markdown file in your data folder, `personality.md`, created from a
neutral default on first start. It is yours: edit it in the dashboard or in any editor, or delete
it to get the default back. It is part of every system prompt, so treat it the way you would treat
anything else you tell Nox — do not put passwords or data about other people in it.
