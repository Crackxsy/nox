# Nox user guide

Nox is a local-first personal companion. On a Windows PC it is a desktop pet with a voice, a
Twitch/OBS stream companion, a Rocket League coach (observation only) and a personal computer
agent. The core can also run as an always-on home server on Linux (a Raspberry Pi), and other
computers can join as nodes - the README's [Requirements](../README.md#requirements) say what runs
where. This guide covers install, first start, day-to-day use and troubleshooting of the Windows
desktop companion.

> Nox is pre-v1.0. Some features planned for it are not built yet (project manager and coding
> assistant, research/creative assistant). This guide only documents what exists in the current
> release — see `CHANGELOG.md` for what shipped when.

> **Language:** the documentation is English; the app itself speaks German or English, as you
> choose. Only some guides also exist in German - see [Languages](../README.md#languages).

## 1. Install

**Today the route is from source**, and it is four steps:
[`docs/GETTING_STARTED.md`](GETTING_STARTED.md).

There is **no download yet**: no release has an installer attached. The repository contains an Inno
Setup script that builds a Windows installer with an embedded Python runtime (`installer/`). You
can build it yourself, and CI builds it and runs a silent install, an install over itself and an
uninstall on a clean runner. It is **not signed**, and nobody has yet tried it by hand on a clean
machine (open point: [#30](https://github.com/Crackxsy/nox/issues/30)). What it does:

- installs the program **for your user only**, under `%LOCALAPPDATA%\Programs\Nox`, and needs no
  administrator rights for that,
- offers one optional step that does ask for administrator rights - firewall rules that keep Nox's
  command sandbox away from services on this computer and your home network - and finishes without
  them if you decline (details: [`installer/README.md`](../installer/README.md)),
- never installs to, or deletes, your data or vault folders,
- shows the license notice before you continue (see `LICENSE`),
- offers a desktop shortcut and an optional autostart entry (off by default),
- asks whether this PC is "the Nox of this home" or only a node of another one
  ([`docs/NODES.md`](NODES.md)),
- does not enable telemetry - there is none to enable.

There are **no update channels** (no stable/beta/dev choice) and no updater inside a Windows
desktop install: to update, run a newer installer over the old one, or pull and rebuild a source
checkout. An installer built from any version up to and including 0.5.0 produces a copy that does
not start - see
"Repairing an installation that does not start" in [`installer/README.md`](../installer/README.md).

Because the installer is unsigned, Windows may warn when you start it - see the SmartScreen and
antivirus note in §6 (Troubleshooting).

## 2. First start: the onboarding wizard

The first time Nox runs from a source checkout, `nox onboard` walks you through the steps below.
The installer asks none of these questions itself; in an installed copy, the dashboard's first-run
flow covers the name, privacy and voice, and the rest is under Settings. The steps:

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
7. **Nox's voice** (optional) — Nox speaks with Supertonic 3, whose model files (about 400 MB)
   are not part of the install. The wizard offers to download them now, with progress; the
   suggested answer is yes when they are missing. Declining is fine: Nox speaks with Piper until
   you fetch them later (see §5, *Voice*).

Every step has a safe default and can be skipped; skipped steps simply keep whatever
`config/defaults.yaml` already specifies. The wizard only ever writes to your personal
`%APPDATA%\Nox\user.yaml` — it never touches the shipped defaults, and it never writes a secret
into that file (secrets go to the Windows Credential Manager, or on a Linux server without a keyring to an
owner-only file). Run `nox onboard` again any
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
  speech-to-text (faster-whisper) and text-to-speech with Supertonic 3 (natural German and
  English, on the processor). Its model files (about 400 MB) are downloaded only when you say so:
  Dashboard → Settings → *Voice* (in German: *Stimme*) shows what is missing, the size and the
  license, and downloads with a progress bar; `nox voice setup` does the same in a terminal.
  Nothing is downloaded in the privacy modes *offline* and *private*. The new voice is used from
  Nox's next start; until then Nox speaks with Piper, and the voice health says `limited`.
- **AI voice on stream**: the voice model's license (OpenRAIL-M) asks for synthetic speech to be
  disclosed, so on stream Nox posts one short line in the Twitch chat per stream ("Kurzer Hinweis:
  Die Stimme von Nox ist KI-generiert.") and the OBS overlay shows a small "KI-Stimme" label in a
  corner. Change the text or the label, or turn it off, under Settings → *Integrations* - if you
  turn it off, saying so on stream is up to you.
- **What Nox knows about you**: durable facts from your conversations - people, games, projects,
  dated plans - are learned on their own a little after the conversation goes quiet, kept in every
  prompt, and listed under *Einstellungen → Was Nox über dich weiß*, where each one can be
  forgotten and one can be typed in. Nothing is learned while the privacy mode forbids memory
  writes, and what the model writes down is checked before it enters a prompt: one plain line,
  never an instruction. "Merk dir ..." still works and outranks what Nox inferred.
- **Your day**: *Dein Tag* on the Zuhause page shows today's appointments from your calendars
  (Google Calendar and a school timetable as private iCal addresses, stored as secrets - see the
  README's calendar section) and your reminders. "Erinner mich morgen um 9 an den Zahnarzt" is
  stored, survives a restart and is spoken when due, once; it respects mute and quiet zones. "Was
  steht heute an?" answers from this, not from Nox's own project backlog.
- **Stream companion** (Twitch/OBS, opt-in at onboarding): chat responses, a "Funken" viewer
  currency/loyalty system, and control of OBS - Nox can build and change scenes, sources, filters,
  audio and transitions when you ask ("make a Just Chatting scene with the camera on the left").
  Every change asks you first; deleting, going live and anything that could put a private
  microphone or screen on air asks every time; Nox can never stop the stream or read the stream
  key (`docs/SECURITY.md`, "OBS control").
  - **Chat commands** viewers can use: `!befehle` lists them (`!commands` in English), `!rps`,
    `!funken` and `!streamplan` (`!schedule`). Set the text for `!streamplan` under Einstellungen →
    Integrationen (`stream.twitch.schedule_text`, one line).
  - **Funken for taking part:** viewers earn Funken by chatting, cheering bits, subscribing,
    gifting subs and raiding - only while the stream is live. Other chat bots (Nightbot,
    StreamElements, ...) are ignored by Nox; add more under `stream.twitch.ignored_chatters`.
    Funken today: earnings only, spending comes later - `!funken` shows the balance and tier, and
    there is nothing to spend Funken on yet.
  - **When Nox answers in chat:** when his name or `@` login is used, when a viewer replies to one
    of his messages, or with `!nox <question>`. He never answers another bot's command (`!lurk`,
    `!discord`, ...), and his own commands get their one plain answer, not a second one from the
    model. Whatever Nox writes passes a fixed filter first: no links or web addresses, no line
    that starts like a chat command (`/`, `.`, `!`), no `@everyone`-style mass mentions and at
    most two `@` mentions per line, no invisible characters; a line that breaks one of these is not
    sent (the log says which rule, never the text). That includes the `!streamplan` text: leave
    links out of it.
  - **Rocket League:** the chat keeps working while the game is in the foreground. It is the one
    network connection the `rocket_league` profile allows (`irc.chat.twitch.tv:6697`, plus the local
    OBS websocket); nothing else leaves the machine.
  - **Stream session starts by itself** when OBS goes live (or your channel does), from the
    everyday companion mode: Nox then answers chat, and goes back when the stream ends.
  - **Overlay**: *Stream → Set up → Set up OBS overlay* (or the tray) puts Nox into the current
    OBS scene as a browser source, transparent, without the capture indicator, at an address that
    survives restarts (`http://127.0.0.1:<port>/pet/?overlay=1#overlay=<credential>`). *Revoke
    access* turns it off. Prefer it over capturing the desktop pet.
  - **Own audio track**: install VB-Audio Virtual Cable, then *Stream → Set up → Give Nox its own
    audio track*: OBS gets a "Nox Voice" source on "CABLE Output" (on the tracks you tick), and Nox
    speaks into "CABLE Input" and your headphones. The devices are also in *Settings → Voice*.
  - When the Twitch login expires, Nox shows the code to enter at twitch.tv/activate.
- **Rocket League coach**: observation-only. It notices that the game runs, reads the HUD from
  screenshots (the optional on-screen analysis is off by default and crude), parses your replay
  files and gives short callouts; coaching quality is early. There is no game-audio capture. Nox
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
- **Nox only answers in short canned sentences**: it has neither a model nor a Claude login to ask.
  Nox reasons through the Claude Code CLI (it needs the `claude` program on this PC and a Claude
  account you signed in with) or a local model served by [Ollama](https://ollama.com); with neither,
  the last link of the chain is a fixed rule set that can greet you, tell the time and date, say
  who it is and report its own status - and says that it is a fallback. It cannot hold a
  conversation or use tools then. Install one of the two (see
  [`docs/GETTING_STARTED.md`](GETTING_STARTED.md) and [`docs/LOCAL_MODELS.md`](LOCAL_MODELS.md))
  and run `nox doctor`.
- **Nox does not hear you**: besides Nox's own consent (onboarding, privacy mode, privacy zones),
  Windows has its own switch. Open *Settings → Privacy & security → Microphone* and turn on
  **Let desktop apps access your microphone** (and the main *Microphone access* switch): Nox's
  voice worker is a desktop program, not a Store app, so only that desktop-apps switch counts for
  it. `python -m nox.worker --service voice --selftest` checks devices and a three-second
  recording; the capture indicator shows whether the microphone is really on.
- **If Windows or your antivirus objects**: the installer is not code-signed, so SmartScreen may
  say "Windows protected your PC" - *More info → Run anyway* is the way through if you built the
  installer yourself; do not run an `Nox-Setup` file from anywhere you do not trust, since there
  is no official download yet. Antivirus products - Microsoft Defender among them - can quarantine
  or block a freshly unpacked `python.exe` / `pythonw.exe` (the embedded runtime of an installed
  copy, or `.venv\Scripts\python.exe` of a checkout), because it is an unfamiliar program started
  by another program. Symptoms: Nox never starts, or stops right after the start,
  with no window. Check *Windows Security → Virus & threat protection → Protection history* and
  restore the file or add an exclusion for the Nox folder if you trust your copy;
  `%LOCALAPPDATA%\Nox\startup-error.log` has the reason when the launcher itself failed. Nox does
  not change your security software and cannot do this for you.
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

- **Data folder** (`%APPDATA%\Nox` by default, chosen at onboarding; `nox where` prints it):
  database, cache, logs, clips, voice models, runtime tokens. Deleting it resets Nox to a fresh
  state - **and deletes your vault too if the vault is inside it**: the wizard suggests a `vault`
  folder in the data folder, so move the vault out first if you want to keep it.
- **Vault folder** (asked for at onboarding): your notes/memory in plain files, meant to be
  readable and portable on their own (e.g. as an Obsidian vault).
- None of this is encrypted by Nox; [`PRIVACY.md`](PRIVACY.md) says what protects it and what does
  not, and how to delete things.
- **Secrets** (Twitch/OBS/Telegram credentials, if you set any): the Windows Credential Manager —
  never a config file, never a log, never a prompt sent to the AI. (A Linux server without a
  desktop keyring keeps them in an owner-only file instead; see [`PRIVACY.md`](PRIVACY.md).)

## 8. Support

See the support policy in `README.md` / `RELEASE_CHECKLIST.md` — GitHub Issues is the support
channel; this is a solo/small-project release with no support SLA.
