# Nox user guide

Nox is a local-first personal companion for Windows: a desktop pet with a voice, a Twitch/OBS
stream companion, a Rocket League coach (observation only), and (later) a project/coding
assistant. This guide covers install, first start, day-to-day use, and troubleshooting.

> Nox is pre-v1.0. Some features described in the Product Requirements Document are not built yet
> (project manager/coding assistant, research/creative assistant). This guide only documents what
> exists in the current release — see `CHANGELOG.md` for what shipped when.

## 1. Install

**Today the route is from source**, and it is four steps:
[`docs/GETTING_STARTED.md`](GETTING_STARTED.md).

An Inno Setup script that builds a Windows installer with an embedded Python runtime is in the
repository (`installer/`), but it has not been signed, and it has not been proven on a machine
that is not the one it was written on. So there is nothing to download yet. What it will do
when there is:

- install the program under `%ProgramFiles%\Nox` (or a folder you choose),
- never install to, or delete, your data or vault folders,
- let you choose stable/beta/dev update channels,
- show the project license before you continue (see `LICENSE`),
- offer a desktop shortcut and an optional autostart entry,
- not enable telemetry - there is none to enable.

## 2. First start: the onboarding wizard

The first time Nox runs, `nox onboard` (or the installer's first-run step) walks you through:

1. **Nox name** — what your instance calls itself. Defaults to "Nox", editable.
2. **UI language** — German or English.
3. **Data folder and vault folder** — where Nox keeps its database/cache/logs (data folder,
   `%APPDATA%\Nox` by default) and your notes (vault folder, kept as a separate, user-owned
   location — never inside the program folder).
4. **Twitch / OBS / Telegram** (all optional, all skippable) — connect a stream chatbot, an OBS
   websocket connection for scene control, or a Telegram bridge. Declining any of these just means
   that integration stays off; you can run `nox onboard` again later, or use `nox secrets set
   <name>` directly, to add one.
5. **Microphone and camera consent** — explicit, separate yes/no for each. Neither is silently
   turned on; the capture indicator (see §4) always reflects the real state and cannot be hidden.
6. **AI backend** — Claude Code (cloud reasoning, with local Ollama as a fallback) or an
   Ollama-only local setup. The wizard actually probes both backends before you choose (not a
   guess) and shows you which one is really available on your machine right now.

Every step has a safe default and can be skipped; skipped steps simply keep whatever
`config/defaults.yaml` already specifies. The wizard only ever writes to your personal
`%APPDATA%\Nox\user.yaml` — it never touches the shipped defaults, and it never writes a secret
into that file (secrets go to the Windows Credential Manager only). Run `nox onboard` again any
time to change these answers.

## 3. Starting and stopping Nox

- **Normal start**: the supervisor (`nox supervisor`, or the Start Menu shortcut) spawns the core
  process and the desktop shell, and restarts them if they crash.
- **Tray icon**: shows Nox's status, offers the dashboard URL, privacy-mode switch, and quit.
- **Dashboard**: the tray menu's "Open dashboard" link opens Nox's web dashboard in your browser;
  the access token travels in the URL fragment (never logged, never in a query string you'd paste
  into a chat).
- **Kill switch ("Notaus")**: instantly cancels all AI activity, stops text-to-speech, stops every
  worker/plugin, turns capture off, and shows the pet as unavailable. Trigger it via:
  - the hotkey `ctrl+alt+shift+k` (works even if the core has hung — the supervisor owns this path),
  - the tray menu,
  - the dashboard,
  - the pet's own menu,
  - the spoken phrase **"Nox, Notaus"** (matched locally by the voice worker, never routed through
    the AI).
  A kill you triggered yourself resumes with a normal click/hotkey. A kill the security layer
  triggered on its own (tamper detection, a broken audit chain, panic mode) requires your PIN to
  resume — this is deliberate, not a bug.

## 4. Privacy modes, zones, and the capture indicator

Nox has four privacy modes: **FULL**, **BALANCED** (default), **PRIVATE**, **OFFLINE** — see
`PRIVACY.md` for exactly what each one changes. Switch modes from the tray, the dashboard, or the
hotkey. Moving to a *more* private mode never asks for confirmation; moving back to FULL always
does.

**Privacy zones** are apps/windows Nox is configured to never look at (banking, password
managers, email, private chats, personal documents, Discord by default) — while one is in the
foreground, capture, screenshots, clipboard reads and memory writes about it are all off, and the
pet shows a `privacy` state.

The **capture indicator** in the shell always shows the real microphone/screen/camera state. It
cannot be turned off or hidden by configuration — if something is being captured, you will see it.

## 5. Core features

- **Desktop pet**: a small always-on-top window with a stable personality, mood, and voice
  (local TTS/STT by default; barge-in supported).
- **Giving the pet a treat**: shift-click the creature, or press Shift+Enter while it has focus. It
  eats, and it cheers up a little more than it does from an ordinary click. It curls up when it
  sleeps and lies down when it is bored, on its own.
- **Choosing the creature**: Dashboard → Settings → *Nox window* → *How Nox looks* (in German:
  *Nox-Fenster* → *Aussehen von Nox*). Seven photographed creatures - Meereswolf, Eulenfuchs,
  Katzendrache, Mottenkatze, Kaninchenkatze, Koala-Flughund and Chamster - each breathe, blink,
  flick an ear, and have their own pictures for sleeping, lying down and eating; a preview shows the
  one you picked. The five drawn shapes need no pictures at all. The change applies at once,
  without a restart.
- **Voice**: push-to-talk (`ctrl+alt+space` by default) or the wake word "Nox"; local
  speech-to-text (faster-whisper) and text-to-speech (Piper).
- **Stream companion** (Twitch/OBS, opt-in at onboarding): chat responses, a "Funken" viewer
  currency/loyalty system, scene-aware behavior via a narrow, read-mostly OBS tool surface (scene
  switching is a confirmed action; there is no scene/source-delete or stream-stop tool — see
  `SECURITY.md`).
- **Rocket League coach**: observation-only — screen/HUD capture, game audio, replay files. Nox
  never sends input to the game and never reads or injects into its process memory; this is a
  hard, code-level boundary (see `SECURITY.md` §Rocket League boundary), not a setting.
- **Presets**: one phrase that reaches several systems at once - lights and scenes through
  your own Home Assistant, Nox's mode, and programs you registered yourself. A preset can
  also start at a time of day or when a game launches. Nothing is configured by default and
  the language model can only name a preset, never assemble one: see
  [`docs/PRESETS.md`](PRESETS.md).
- **Typing for you**: Nox can type text into one window - it brings the window to the front first,
  checks it is really there, and asks you before it does any of it. Letters only: no key
  combinations, no mouse. And nothing at all while a game is running, anywhere on the machine, not
  just when the game is the window in question.
- **Windows and programs**: Nox can tell you what is running and what is open, bring a window to
  the front, minimise it, or ask a program to close the way its X button does. Closing and ending a
  program ask you first. Windows' own session processes, Nox itself and a running game are out of
  bounds entirely - not "ask first", but refused.
- **Files**: Nox can look at and change files in the folders you name, and nowhere else. The list
  ships empty, so a fresh installation reaches nothing outside its own vault. Writing asks first,
  and deleting only ever moves a file to the Recycle Bin - there is no delete you cannot undo. See
  [`docs/FILES.md`](FILES.md).
- **Tools in a conversation**: anything you can do with a button on the dashboard, you can ask
  for in the chat or say out loud - the same tool, the same permission check, the same audit
  entry. Nox is only offered the tools your active profile actually allows, so it cannot promise
  something the profile forbids. Which tools an answer used is shown under that answer.
- **Asking what Nox can do**: "was kannst du gerade?" is answered from a catalogue rather than
  guessed. It distinguishes what is not built, what this profile forbids, what is missing a token,
  and what Nox will never do - and it says "I have no entry for that" instead of a false no.
- **Board**: when numbers are clearer looked at than read out, Nox draws them - a table, a bar or
  line chart, a list of facts, or a note - and they appear on the Board page. Nox can only choose
  between those five shapes and fill in the values; it cannot send the dashboard anything to
  execute.
- **Plans**: for work with several steps, Nox writes down what it intends to do and waits for you
  to approve it. Approved plans run in the background, survive a restart and pause while a game is
  running. A plan that was interrupted mid-step says so rather than repeating that step - see
  [`docs/PLANS.md`](PLANS.md).
- **Asking Nox to change itself**: Nox can write a proposed change to its own source on a branch in
  a checkout you name (`extend.workspace`, empty by default), run your tests, and show you the diff.
  It never merges, never pushes and never restarts itself - reading the diff and deciding is yours.
  It needs the coding profile, because the plugin that writes the change only runs there - see
  [`docs/EXTEND.md`](EXTEND.md).
- **Project manager / coding assistant**, **research/creative assistant**: planned, not part of
  this release.

## 6. Troubleshooting

- **`nox doctor`** prints an honest environment report: config load status, database/vault paths,
  each AI provider's real health (available/limited/unavailable, with a reason — never a guess),
  and whether the optional voice/shell libraries actually import.
- **Logs**: `%APPDATA%\Nox\logs` (structured JSON, PII-filtered, rotated daily).
- **Runtime state**: `%APPDATA%\Nox\runtime` (session tokens, IPC config, supervisor token) — safe
  to delete while Nox is fully stopped if something looks stuck.
- **Nothing responds**: use the kill switch, then restart via the supervisor. If the core keeps
  crash-looping, check `%APPDATA%\Nox\logs` for the boot error `nox doctor` would also have
  caught.
- **AI backend shows "unavailable"**: `nox doctor` tells you why (not logged in, model not pulled,
  server not running, etc.) — Nox degrades to the next provider in its fallback chain rather than
  failing silently; it never pretends a backend works when it does not.
- **First start is busy for a minute or two**: the memory extension indexes your whole vault once
  (one embedding request per note chunk, in the background). Chat and voice already work while it
  runs; later starts only re-index changed notes.
- **`token_acl_failed` in the log / config file not where the guide says**: your `.venv` was made
  with the *Microsoft Store* Python. Store apps see a virtualized `%APPDATA%` (their files really
  live under `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation...\LocalCache`), so tools outside
  the package cannot find them. `nox doctor` warns about this. Fix: install Python from
  python.org, delete `.venv`, run `uv sync` again.
- **Shell shows "Website nicht erreichbar" / stays offline**: the core was not up yet (or was
  restarted by the supervisor). The shell retries every 3 s and reloads the pet page with the new
  session token on its own; if it never connects, check the core log for a boot error.

## 7. Where your data lives

- **Data folder** (`%APPDATA%\Nox` by default, chosen at onboarding): database, cache, logs,
  runtime tokens. Deleting it resets Nox to a fresh state but does **not** touch your vault.
- **Vault folder** (chosen separately at onboarding): your notes/memory in plain files, meant to
  be readable and portable on their own (e.g. as an Obsidian vault).
- **Secrets** (Twitch/OBS/Telegram credentials, if you set any): the Windows Credential Manager
  only — never a file, never a log, never a prompt sent to the AI.

## 8. Support

See the support policy in `README.md` / `RELEASE_CHECKLIST.md` — GitHub Issues is the support
channel; this is a solo/small-project release with no support SLA.
