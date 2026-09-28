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
  configurable, see the table below) — never uploaded anywhere except as part of a request to
  whichever AI provider you chose, under the rules below.
- **Logs** — local, JSON-structured, filtered for personally-identifying content and secrets
  before being written, rotated (14 days by default).
- **Memory / vault notes** — local files in your chosen vault folder, meant to be yours: readable,
  portable, and never written to while a privacy zone is active for the thing being discussed, in
  PRIVATE mode, or in a profile that remembers nothing (`work`) — see "Profiles" below.
- **Raw audio is never persisted**, in any mode, at any privacy level. Only the transcript (if
  transcription is enabled) is kept, under the retention window above.
- **Secrets** (API keys, OAuth tokens, passwords for Twitch/OBS/Telegram) — the Windows Credential
  Manager only. Never a config file, never a log line, never included in a prompt sent to an AI
  provider.
- **Audit log** — a local, tamper-evident (hash-chained) record of security-relevant events
  (mode/privacy changes, kill switch, permission decisions, config changes, tool executions with
  side effects, memory deletions, plugin lifecycle). The audit log itself never contains secrets
  or raw private content.

### Retention, table by table

A retention job in the core deletes expired rows a minute after every start and then every six
hours; each run is recorded in the audit log, and the dashboard's health page shows the last
successful run as `retention`. Changing a setting applies from the next run. The database lives
in `<data_dir>/database/nox.db`.

| What (table) | Kept for | Setting |
|---|---|---|
| Conversation turns (`turns`) | 7 days after they were said; also every turn older than the current setting | `privacy.retention.raw_transcripts_days` (`0` = no expiry) |
| Stream chat (`chat_events`) | 7 days | `stream.chat.retain_raw_text_days`, applied when the message is stored |
| Viewers (`viewers`), with their notes (`viewer_memory`) and Funken ledger (`funken_ledger`) | 12 months after the viewer was last seen | `privacy.retention.viewer_data_inactive_months` |
| Health history (`health_history`) | 365 days | `privacy.retention.metrics_days` |
| Proactive notifications (`proactive_notifications`) | until their own expiry; dismissed ones 30 days | `proactive.notifications_retention_days` |
| Memory items (`memory_items`) | until you delete them; an item stored with its own expiry is removed after it | per item |
| Note rollback copies (`vault_note_versions`) | 30 days | `memory.retention_note_version_days` |
| Rocket League events (`rl_events`) | 180 days | `rl.retention.events_days`, applied when recorded |
| Rocket League matches (`rl_matches`) | 365 days after the match | `rl.retention.matches_days` |
| Rocket League vision detections (`rl_vision_frames`) | 6 hours | `rl.vision.detections_retain_hours`, applied when recorded |
| Temporary permission grants (`temporary_grants`) | until they expire or are revoked | each grant's own expiry |
| Phone pairing codes (`remote_pairings`) | until the code expires (5 minutes) | `remote.pair_code_ttl_s` |
| State checkpoints (`state_checkpoints`) | the newest 200 | fixed |
| Database backups taken before an upgrade (`<data_dir>/backups`) | the newest five, and none older than 30 days | fixed |
| Log files | 14 days, rotated daily | `logging.retention_days`, else `privacy.retention.logs_days` |

Kept on purpose, never deleted by the retention job:

- **The audit log** (`audit_log`) — append-only and hash-chained; deleting from it is exactly the
  tampering it exists to detect. `security.audit.retention_days_*` are not applied yet. The record
  of commands from a paired phone (`remote_audit`, the command verb only) is kept as well.
- **Replay files and their index** (`rl_replays`), **match analyses** (`rl_vision_analysis`),
  **sessions, tasks and stream sessions** — deleted only when you delete them.

A backup is a full copy of the database at the moment it was taken, so until it is deleted it can
still hold rows the retention job has since removed from the live database - at most 30 days.

If Nox finds its database damaged at start, it renames the file (and its `-wal` journal) to
`nox.db.corrupt-<time>` next to the original, starts with an empty one, and reports it in health
and as the first entry of the new audit log. The damaged file is kept for you to inspect or
delete; Nox never deletes it.

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

Nox defaults to `voice.stt.listening_mode: ptt_only`: the microphone is opened only while
push-to-talk is held, so nothing is captured unless you ask for it. With `continuous`,
microphone audio passes a local wake-word gate
first; audio that does not pass the gate is never transcribed. Short segments still reach the
recogniser for the voice kill-phrase watchdog, and those transcripts are discarded without
becoming an event. While Nox itself is talking, speech that is not louder than its playback is
treated as Nox's own echo and is not transcribed either. Within the conversation window after you
addressed Nox, what you say next is treated as addressed to Nox without the wake word
(`voice.stt.conversation_window_s`, 0 turns it off).

No speech model is downloaded without you asking: the Whisper and Kokoro models are fetched only
by `python -m nox.worker --download-whisper` / `--download-kokoro`, which you run yourself.

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

In PRIVATE and OFFLINE, integrations that talk to anything beyond this machine (Telegram, Twitch,
Home Assistant) and the coding assistant (which drives the Claude Code CLI)
are not running at all: the plugin manager stops them when you switch and starts them again when
you switch back. An integration that only talks to this machine (the OBS websocket) keeps running.

## Profiles

A profile (`companion`, `work`, `stream`, …) says what a kind of work may do at all; the privacy
mode says what the moment allows. Nox applies **both**, and the stricter one wins:

- **Cloud AI** is used only when the profile has `cloud_allowed: true` *and* the privacy mode is
  FULL or BALANCED. The `work`, `offline` and `rocket_league` profiles never send a conversation to
  a cloud provider, whatever the privacy mode — not for a plain question, not for a "denk mal
  gründlich nach" that would otherwise go to the reasoning model, and not when the local model is
  down. The Claude Code CLI itself refuses to start (it also skips its login check) while the
  cloud is blocked.
- **Memory** — conversation history, memory items and vault notes — is written only when the
  profile has `memory_writes_allowed: true` *and* the privacy state allows it (not PRIVATE, no
  privacy zone that closes memory, no kill switch). In the `work` profile nothing is remembered.
- **Integrations** (Ollama, Claude Code, Telegram, Home Assistant, Twitch, OBS) run only when the
  profile lists them in `integrations_allowed`. The deterministic rules fallback is part of Nox
  and always available.

A profile switch — from the dashboard, or automatically when Rocket League starts — takes effect
with the next request; no restart is needed.

## Privacy zones

You can mark apps/windows (by process name or title pattern) as zones Nox should never look at —
banking, password managers, email, private chats, personal documents, and Discord are zoned by
default. The built-in title patterns match whole words and known names ("Bank", "Online-Banking",
"Sparkasse", "KeePass"), not every word that contains them, so "Datenbank" or "Steuerung" is not
a zone; your own patterns keep exactly the meaning you give them. While a zone is the foreground window: no capture, no screenshot, no clipboard read, and
no memory write referencing it. Zone detection happens locally (it only looks at the foreground
window's title/process name) and that detection itself never leaves the machine.

When Nox **cannot see** the foreground window, it assumes the worst rather than the best: it
enters the reserved `unobservable` zone. That is permanent under a Linux Wayland session (Wayland
lets no application read the active window), lasts on macOS until you grant the Accessibility
permission, and applies on any machine without a graphical session. The dashboard's health page
names the reason. Details per platform: [`PLATFORMS.md`](PLATFORMS.md).

What the `unobservable` zone closes is your choice, `privacy.unobservable_policy`:

| Setting | Screen, camera, screenshots to the cloud, clipboard | Microphone, memory writes |
|---|---|---|
| `screen_only` (default) | closed | open |
| `strict` | closed | closed |

The reasoning behind the default: what an unseen window can leak is what is on the screen, not
what you say to Nox — so voice keeps working, and what you tell Nox can be remembered, while
everything that looks at the screen stays shut. A **real** zone (a banking page or a password
manager Nox *can* see, or one recognised by its process name) always closes everything, including
the microphone, whichever setting you choose. Unprompted speech (the greeting, hints) and phone
notifications stay quiet while any zone is in force, the `unobservable` one included.

**Zones do not depend on the other sensors.** Switching the awareness sensors off
(`sensors.enabled: false` — idle time, system load, game detection, window history) leaves the
foreground check for zones running; it then records nothing and only feeds the zones. The one
switch that turns window zones off is `privacy.zones_enabled: false`, a `privacy.*` setting, so
changing it in the dashboard needs your PIN when one is set, and the `sensors` health check says
"privacy zones switched off" for as long as it is off. Path zones for vault notes (personal
documents and the like) are not affected by it.

## How to inspect or delete your data

- **Vault**: it is a folder of plain files you already own — open it in any editor, or delete
  individual notes yourself. Nox never deletes vault content silently.
- **Transcripts, logs, cache, database**: all under your configured data folder
  (`%APPDATA%\Nox` on Windows, `~/Library/Application Support/Nox` on macOS, `~/.local/share/nox`
  on Linux, by default); delete the folder (while Nox is stopped) to reset to a clean state
  without touching the vault.
- **Secrets**: `nox secrets delete <name>`, or remove them directly from the Windows Credential
  Manager, the macOS Keychain or your Linux keyring (entries under the service name `nox`).
- **Audit log**: inspectable from the dashboard; resetting it requires your PIN (it is a security
  control, not a convenience feature).

## Your home

Smart-home control talks to **one** address: the Home Assistant instance you configured
(`home.host` / `home.port`). There is no vendor cloud, no manufacturer account and no bridge
service anywhere in this feature, and no third-party smart-home library in the codebase: the
plugin speaks Home Assistant's documented WebSocket API directly, and the "Verbindung testen"
button is one REST request from the core through the same egress guard as everything else.

**What is read.** States are read on demand: when you open the Zuhause page, when you give a
command, and when Nox answers a question that needs them. On top of that the plugin subscribes to
exactly one Home Assistant event type, `state_changed`, so the page can show a light that someone
switched by hand. Only an allow-list of attributes ever leaves Home Assistant (brightness, target
temperature, volume level, device class and similar); a media player's `media_title` — what someone
is watching — is not on it.

**What is never read.** Home Assistant's `person` and `device_tracker` domains, which say who is
home and where a phone is, are filtered out entirely. So are `lock`, `alarm_control_panel` and
`valve`, and any `cover` that reports itself as a garage, gate or door — those are a safety
boundary as well as a privacy one (see below).

**What is stored.** Nothing. No state of your home is written into the vault, into the memory
index, into the audit log's details, or into a prompt sent to an AI provider — unless you asked a
question that needs it, in which case the entities involved are part of that one request and are
not kept afterwards. The live event stream is ephemeral: it reaches the open dashboard page and is
never persisted.

**Privacy modes cut the connection.** PRIVATE and OFFLINE mean "nothing leaves this machine except
allow-listed local services", and smart-home control is not an exception to that. The plugin
refuses to connect in those modes — in code, before any socket is opened — and reports itself
`unavailable` with that reason. This holds even for a Home Assistant on loopback, which the egress
guard alone would let through.

**The hard boundary.** Nox has no tool for a door lock, an alarm panel, a water or gas valve, or a
garage door. Those entities never appear in a listing and naming one explicitly is refused with a
logged reason. It is enforced in `src/nox/home/boundary.py` and in the plugin that uses it, not by
an instruction in a prompt: a control a text field can talk its way past is not a control.

**Which rooms.** `home.areas_allowed` limits Nox to the rooms you name (empty = every room). It is
checked in the plugin worker before a service call is built, so it is a real restriction rather
than a hint to the model.

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
