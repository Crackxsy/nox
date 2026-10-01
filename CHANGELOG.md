# Changelog

All notable changes to Nox are documented here, in [Keep a Changelog](https://keepachangelog.com/)
style. Versioning follows [Semantic Versioning](https://semver.org/). Every release is tagged;
changelog entries are reviewed before merge, not added after the fact.

Nox is pre-v1.0 as of this writing. See `docs/RELEASE_CHECKLIST.md` for what v1.0 itself requires.

## [Unreleased]

### Added
- **A stalled event loop is reported with the code that stalls it.** When the core's loop does not
  answer for 3 s, `core.loop_stalled` names the module, function and line it is stuck in, and
  `core.loop_resumed` says how long it lasted - instead of a silent gap in the log.

### Fixed
- **The core restarted itself over and over on a cold start.** With the data on a spinning drive,
  single install steps of the boot stalled the core's event loop for 13-16 s while the voice worker
  and the language model were loading from the same drive. The supervisor counted that as ten missed
  heartbeats and killed the core - which started the same disk-bound boot again, so the next one
  stalled too. The heartbeat now says whether the core is still booting, and while it is, a silence
  only counts as a hang once it outlasts `supervisor.boot_grace_s`; a core that has started is
  watched as strictly as before. The Rocket League extension also no longer imports its vision and
  callout modules on the event loop.
- **The database health check stalled the core every 30 s on a busy drive.** It ran a full
  `PRAGMA integrity_check` - every page of the database - under the lock the event loop needs for
  its own queries. Warm that costs 40-150 ms; on a cold spinning drive it held the lock long enough
  for the supervisor to kill the core. The periodic check now only asks whether the database
  answers; the full integrity check still runs once, when the database is opened.
- **The audit-chain health check re-hashed the whole audit log on the event loop** every 30 s. It
  still verifies the whole chain - it is how tampering is noticed - but now in a worker thread, with
  30 s instead of 5 s before a slow drive is mistaken for a broken chain.
- **The process list showed a blank name** for protected system processes such as "Registry",
  whose name Windows does not hand out. They are now listed as "(protected system process)".

## [0.4.0] - 2026-10-01

### Added
- **Seven creatures to choose from, each one complete.** Meereswolf, Eulenfuchs, Katzendrache,
  Mottenkatze, Kaninchenkatze, Koala-Flughund and Chamster, each with a deformation rig - it
  breathes, blinks, flicks an ear, turns its head, and moves its tail or wings - and its own art for
  sleeping, lying down and eating. They come from the original creature concepts; the key poses were
  generated against each creature's own picture on a green screen and keyed out.
- **The creature is picked from a list in the dashboard**, with a picture of the one you chose under
  it, instead of a text box that expected you to know to type `sprite:koalaflughund`. It applies
  without a restart. A configuration written before the list existed still loads; an unknown value
  falls back to the plain shape and says so in the log.
- **Tools to add a creature**: `ui/pet/scripts/cut_out.py` keys a generated picture off a
  checkerboard, a flat colour, a lit studio wall or a chroma screen; `make_variant.py` turns the
  cut-out and a landmark file into a whole rigged variant; `ui/pet/scripts/README.md` walks through
  it, including how to ask an image generator for pose art it can actually use.

### Fixed
- **The pet turned into another animal when it fell asleep.** The Meereswolf's base picture was a
  wolf and its three pose pictures were a hamster, so sleeping cross-dissolved one creature into the
  other on screen. Nothing objected: every file was valid. The hamster is now its own creature, the
  Chamster, and a test reads the variants off disk and checks each one is internally consistent.
- **No creature ever lay down when bored.** The pose table looked the mood up under a key that only
  ever held the activity, so the lying-down art was unreachable. The user guide had been promising
  it all along.
- **A pet that started asleep sat up first**, for a quarter of a second, before curling up: the first
  pose was dissolved into from a frame that was never drawn.

## [0.3.0] - 2026-10-01

### Added
- **The default local model could not drive Nox's own tools, and now one that can is the default.**
  `scripts/probe_models.py` asks every model Ollama has the real question - the real offer text, the
  real parser from `nox.ai.tooluse`, four cases including one where the right answer is *not* to
  reach for a tool. `llama3.2:3b`, which was the shipped default, hit 1 of 4 at 14.6 seconds an
  answer: it writes a friendly paragraph where a tool directive belongs, so the local fallback could
  talk and do nothing. `qwen3:4b-instruct` hits 3 of 4 at 2.9. Measured on the development machine
  (GTX 1060 6 GB, Ryzen 7 7800X3D), and the probe ships so anyone can measure their own.
  [`docs/LOCAL_MODELS.md`](docs/LOCAL_MODELS.md) has the table and two findings behind it: a bigger
  model is not the win, because the larger open models reason before answering and an 8B that fits
  this card takes 32.7 seconds; and "does not fit the VRAM" is survivable while "does not fit the
  RAM" is not - a model streamed from an SSD needs every layer for every token.
- **Nox can propose a change to itself, and cannot apply one.** `extend.propose` starts a branch in
  a checkout you named, asks the `coding` plugin to write the change there, runs your test command,
  and hands back the diff and what the tests said (`src/nox/extend/`). There is deliberately no
  `extend.apply`: merging a branch and restarting Nox on the result are things a person does with
  their own git, which is what keeps "Nox can extend itself" from meaning "Nox can change what it is
  while you are not looking". `extend.workspace` ships empty, so out of the box there is no
  repository it may touch and the tool says which setting to fill in. High risk, so every profile
  asks first - and the confirmation shows the *intent*, because "add a tool for reading my calendar"
  is a question a person can answer and an identifier is not. The git surface is four verbs with the
  absences as the point: no merge, no push, no reset, no branch deletion, and the user's checkout is
  put back on *every* path out, including the ones that raised - which is tested against a real
  temporary repository rather than a mock, because a mock would have granted that for free.
- **The installer offers a desktop shortcut**, ticked by default. It made a start menu entry and an
  optional autostart, which means the first thing somebody does after installing - look for it - only
  worked if they already knew it was called Nox.

- **The creature curls up, lies down and eats.** The key-pose path shipped working and the art for
  it did not exist, so `poses` in the Meereswolf rig was an empty object and every pose request
  quietly did nothing. Three cut-outs now fill it: it curls up when it sleeps, lies down when it is
  bored, and eats when you give it a treat - shift-click it, or Shift+Enter while it has focus, so
  the second interaction is keyboard-reachable for the same reason the first one was made to be. A
  treat lifts affection and mood more than a pat does and lifts the one thing a pat cannot, energy.
  Making the art usable took two fixes that were only visible by watching the pet: a key pose is a
  *different picture*, not a deformation of the base one, so it is no longer skinned - the mesh's
  bone weights describe the base photograph, and turning the head bone loose on a curled-up body
  smears the drawing instead of animating it. And the eyes, which are separate layers placed by the
  skeleton, now fade exactly as the base drawing does; at full alpha over a curled body they were
  two eyes hanging in the air above it. `make_pose_frames.py` prepares a cut-out for the rig: it
  downscales in premultiplied alpha, because averaging raw colour across a transparent edge drags
  the colour of nothing into the silhouette and leaves a pale halo, and it shifts each pose down
  until its lowest pixel meets the base drawing's - which is what makes the dissolve read as the
  creature lying down *where it was standing* rather than sinking through the floor.
- **Nox can type into a window, and the boundary around that is narrower than the request.** The
  input-synthesis prohibition was "no file anywhere may import one of these APIs". It is now
  window-scoped, as asked: `desktop.type_text` brings one window to the front, checks it really got
  there, and types - high risk, so every profile asks first. `SendInput` lives in exactly one file
  (`src/nox/desktop/keyboard.py`) and that file refuses to send anything at all while a watched game
  process is running *anywhere on the machine*, which is stricter than refusing the game's window:
  Rocket League's boundary is not only about where input lands, and a process that observes a game
  and synthesises input is the shape an anti-cheat is right to distrust. The cost of being wrong
  there is an account, not a failed request. It types characters (`KEYEVENTF_UNICODE`), which cannot
  express Alt+F4 or Ctrl+A - the damage in synthetic input is almost never in the letters. There is
  no mouse input, and `game.input.send` stays in the immutable hard-prohibition list. The CI guard
  was narrowed rather than relaxed: process-memory calls and the automation libraries are still
  forbidden everywhere, `SendInput` must appear in that one file and nowhere else, *and* that file
  must still contain its game guard - an exemption nobody re-checks is a hole. The guard caught its
  first thing immediately: this module's own docstring, which had named the forbidden APIs in prose.
  A grep cannot tell a mention from a call, so the prose gave way. Not one test sends a keystroke;
  every one of them asserts something that did not happen.
- **Nox can draw, and it draws data rather than code.** `view.show` puts a table, a bar chart, a
  line chart, a list of facts or a plain note on a new Board page in the dashboard
  (`src/nox/views/`, `ui/dashboard/src/pages/Board.tsx`). The tempting design is to let the model
  write HTML or SVG and put that on the page, which is an injection hole with extra steps: the
  dashboard would be rendering whatever a language model was talked into producing. So the model
  never sends markup. It picks one of five shapes, fills in values, and the page owns the rendering -
  a bar is a div with a width, a table is a table, text is text, and React escapes all of it. The
  shapes are a discriminated union with `extra="forbid"`, so a table sent with a `bars` list is
  refused rather than silently stripped, a row that does not fit its columns is named, and a line
  chart with more values than labels is named too. Every list is bounded, because a view is
  something a person looks at and four hundred rows is not that. The wire input is deliberately
  flat: the tool offer in the prompt lists argument names, not nested schemas, so a single `view`
  object would have shown up as `view: object` and the five shapes would have been invisible to the
  model. The strictness lives one layer in, which is also what lets the tool say *which* field was
  wrong - the executor answers a failed input validation with a generic "invalid input" on purpose,
  since it must not put user content in a log. The `view.shown` event carries the title and the kind
  and not the view, and the page re-reads the board, the same way the Settings page re-reads its
  snapshot after `settings.changed`. The charts are plain CSS and one inline SVG: a charting library
  would be a dependency, a bundle and a theme to argue with, for five shapes that are a div and a
  polyline.
- **Nox can see what is running and tidy up after you.** `desktop.windows`,
  `desktop.processes`, `desktop.window_focus/minimize/restore/close` and `desktop.process_stop`
  (`src/nox/desktop/`). A model does not know window handles, so the window tools take a title
  fragment - and the case that matters is several matches: "close Chrome" with four Chrome windows
  open reports the candidates and changes nothing, because a guess closes the wrong window and the
  user cannot tell a guess was made. `window_close` posts `WM_CLOSE`, which is what the X button
  does: the program is *asked* and may still offer to save, and a true answer means asked, not gone.
  `process_stop` is the other half of that pair and says so in its own description, because a model
  picking the wrong one of the two costs the user work; it never escalates to a kill when a
  terminate is ignored, since a program that will not go is either busy saving or stuck and guessing
  which is not the code's job. `window_focus` reports whether the window actually ended up in front
  rather than whether the call returned - Windows refuses `SetForegroundWindow` in cases this
  process cannot influence, and saying it worked would be a lie. Three things are out of bounds
  entirely, in code rather than as a risk level: Windows' own session processes, Nox itself
  (including its workers - a tool that can kill the process holding the audit log is a kill switch
  with no record), and the game, because observation-only is not only about input. The gap rows for
  process and window control came out of the capability table, and the remaining desktop gaps were
  renamed to the names their tools will have, so the staleness check can catch the next one.
- **Nox can work with your files, in the folders you name and nowhere else.** `file.list`,
  `file.read`, `file.write`, `file.move` and `file.delete` (`src/nox/files/`), bounded by a
  `files.roots` list that ships empty: a fresh installation reaches nothing outside its own vault,
  and the tools name the setting to add a folder to instead of failing oddly. An empty list means
  nowhere, never everywhere - the other reading is how a guard meant to restrict ends up permitting.
  Paths are resolved *before* they are checked, so `..`, symlinks and Windows junctions are all
  checked against where they really lead; the junction test creates a real junction rather than a
  mock. Writing asks first in every profile that has not said otherwise, never replaces a file
  unless told to and never invents a missing folder, because a typo would otherwise build a tree
  nobody looks in. Reading refuses anything that is not text and caps the size, since a file read
  becomes part of a prompt. Deleting goes through the Windows shell with `FOF_ALLOWUNDO` - the
  Recycle Bin, where it can be got back - and there is no fallback to `os.unlink`: if the shell
  refuses, the file stays. Turning the setting off removes the delete tool rather than making it
  permanent. Each tool reports its path as the permission target, so *writing is fine under
  Downloads, ask me anywhere else* is a profile rule and not a code change. See
  [`docs/FILES.md`](docs/FILES.md).
- **Nox can say what it cannot do.** A capability catalogue (`src/nox/capabilities/`) joins three
  things that were never asked together: which tools exist, what the active profile would decide
  about each of them, and what is missing entirely. Four answers come out of that and stay apart,
  because collapsing
  them is how an assistant ends up claiming things that are not true: a tool can exist and be
  forbidden here, be allowed and unreachable for want of a token, or simply not be built. Asking
  the permission engine for all of that leaves no audit entry, via a new `preview()`; recording
  dozens of decisions for an action nobody took would bury the log it exists to explain. Boundaries
  are marked `forbidden` rather than `missing`, so a door lock does not read like a backlog item,
  and the build fails if a row outlives the capability it describes. `capabilities.check` has a
  fourth answer most assistants leave out: "I have no entry for that", which is not a no.
- **Saying it now does what clicking it does.** The orchestrator had no idea tools existed: it
  streamed text, and the tools Nox owns were reachable only from the dashboard or a preset, so
  "mach das Licht an" produced a sentence about lights and nothing else. Neither provider can be
  given tools natively - the Claude Code CLI runs with `--tools ""` on purpose, because its own
  file and shell tools would sit outside Nox's permission engine entirely - so the request travels
  as one line of text, with the guards where the risk is. A directive counts only as the whole
  answer, and prose followed by one runs nothing and is logged rather than swallowed. The stream is
  held back at the head of every tool-enabled turn so the protocol is never read aloud, including
  when the model keeps asking after the offer was withdrawn. What is offered comes from the
  capability report under the same agent the call will use, and an integration test walks every
  offered tool through the real permission engine to prove none would be refused. The dashboard's
  chat shows which tools an answer used, because "Nox changed something on your machine" does not
  belong only in a log file. Three rounds, then the offer is taken away and the model has to answer.
- **Work that outlives the conversation it started in.** A plan (`src/nox/plans/`) is a few tool
  steps with a title, written down so a person can read them before they run - no command lines, no
  conditions, no loops, no nesting. `plans.propose` writes nothing and is low risk; `plans.start`
  is high risk, so every profile falls through to a confirmation, and that confirmation shows the
  plan's *title* rather than its identifier, because "Start: Downloads sortieren" is a question a
  person can answer. Approving in the dashboard needs no dialog - the click is the dialog - and
  either way every step still meets the permission engine. Plans run on the existing task queue, so
  they survive a restart and pause while a game is running. The step being attempted is written to
  the checkpoint *before* the call: a step is a tool call with side effects, so a plan that comes
  back from a crash records that step as interrupted and stops rather than repeating it. A visible
  gap beats an invisible repeat.
- **The desktop pet is a creature, not a photograph.** A 2D deformation rig (`ui/pet/src/rig/`)
  runs a triangulated mesh over the pet's artwork, driven by a small bone hierarchy (root, body,
  chest, neck, head, muzzle, ears, gill fins, tail), so it breathes, tilts its head, flicks one ear
  and sways its tail continuously instead of cutting between a handful of near-identical stills.
  WebGL where it exists, Canvas 2D where it does not - and the downgrade is logged, not hidden. A
  variant opts in by shipping `variants/<id>/rig.json`, a hand-editable file validated at load with
  errors that name the field; a variant without one keeps its static frames, and any error loading
  a rig falls back to exactly that path.
- **The Meereswolf blinks for real.** Its eyes are now separate alpha-masked layers cut out of the
  source photograph (`ui/pet/scripts/make_pet_layers.py`), with the sockets behind them filled with
  surrounding fur, so a blink is an eyelid closing over an eyeball rather than a dip in brightness.
  Ten clips are authored against the picture - `breathe`, `sleep_breathe`, `blink`, `ear_flick`,
  `head_tilt`, `head_turn`, `tail_sway`, `perk`, `speak_idle` and a `startle` the kill switch
  triggers - blended additively, so a blink can start mid-breath without the breath skipping. The
  moods in `petState.ts` are carried as held postures: ears back when it is frightened, low and
  heavy when it is sad. `prefers-reduced-motion` leaves breathing and the mood, nothing else, and a
  hidden window stops the loop and rebases its clock rather than replaying the lost time.
- **`rig.json` can name key poses** (`sit`, `lie`, `curl`, `eat_0..n`) that the player
  cross-dissolves to while the mesh keeps running. The path ships working; the art for those poses
  does not exist yet, so they are reported as missing rather than faked.
- **`?animate=1`**, a dev-only companion to `?still=1`: the same state presets with the animation
  loop left running and a fixed ambient schedule, so `ui/pet/scripts/render_rig.py` can film a
  rig's motion and two review renders can be compared frame for frame.
- One palette for both front-ends: the dashboard's Apple-style token block now lives in
  `ui/shared/tokens.css` (same names, same values, same `prefers-color-scheme` +
  `data-theme` guards) and the pet window uses it instead of its own dark-only colours. The pet
  therefore follows the OS between light and dark — chips, notes, the status label and the
  capture indicator all do — while the window itself stays transparent, and each creature variant
  finally uses the body lightness it already shipped for the active background.
- The pet can render a **sprite variant**: `pet.variant: sprite:<id>` loads
  `variants/<id>/sprites.json`, validates it, preloads every frame and plays the frame sequences
  the existing state machine selects (idle / listening / speaking / thinking / sleeping, plus
  optional blink), with a 150 ms crossfade between expressions, idle breathing as a subtle
  scale/translate, `prefers-reduced-motion` honoured, and a logged fallback to the procedural
  variant when a manifest or a single frame fails to load. A generated placeholder set ships under
  `variants/placeholder/` so the path works end to end before the real art exists.
- `config.set pet.variant` takes effect without restarting Nox: the shell reloads the pet page with
  the new variant on `settings.changed`, and the pet page subscribes to the same event so it can
  swap the variant in place once the core exposes the value to the `pet` role.
- Kokoro (`kokoro-onnx`) as a second text-to-speech engine next to Piper, selectable with
  `voice.tts.engine: kokoro` and the new optional extra `voice-kokoro`. Same `TtsEngine`
  contract as Piper: sentence-wise streaming, synthesis off the event loop, reported sample rate
  (24 kHz), prepared clips. Model files are resolved from `<data_dir>/models/kokoro/` and are never
  downloaded automatically - a missing file is an `unavailable` health reason that names the path
  and the command, `python -m nox.worker --download-kokoro` (~354 MB, user-initiated, no core and
  no egress guard involved). The default stays `piper`.
- Wake-word gate in front of Whisper: `voice.stt.wake_word_engine: openwakeword|text`,
  `wake_word_model`, `wake_word_threshold`, `wake_window_s` and `conversation_window_s`. While
  push-to-talk is not held and no conversation window is open, a speech segment only reaches
  Whisper if the cheap always-on detector fired - background audio (a video, a game) is dropped
  before transcription instead of filling the queue with 20-40 s of latency and inventing
  languages in noise. Audio the user did not direct at Nox is now never transcribed at all.
- `voice.stt.listening_mode: continuous|ptt_only`. `ptt_only` never opens the microphone
  unless push-to-talk is held; `continuous` stays the default.
- `voice.stt.kill_phrase_watchdog` / `kill_watchdog_max_ms`: because no acoustic model for
  the kill phrase exists, short segments still reach Whisper so "Nox Notaus" keeps working. Such a
  transcript is checked for the kill phrase and then discarded - it never becomes an event and
  never reaches a language model.
- `voice.models_dir`: one root for the Piper, Kokoro and openWakeWord model files (empty =
  `<paths.data_dir>/models`).
- New read-only `security.pin.status {}` -> `{configured}` (roles `shell`/`dashboard`): presence
  only, never the PIN, its hash, its length or its algorithm. The core has required a `pin` on
  `secrets.set`/`secrets.delete` ever since a PIN is configured, but the Settings page never sent
  one, so every credential change failed with "permission denied". The page now shows an inline
  PIN field next to Speichern/Löschen when a PIN is configured, sends it with exactly that one
  request, clears it afterwards and stores it nowhere, and turns a PIN refusal into the honest
  `pin_required`/`pin_wrong` message instead of a raw error line.
- `nox.proactive.store.NotificationStore` is now backed by a `proactive_notifications`
  table (migration `0010_notifications`) when constructed with a database, so notification history
  survives a restart; `db=None` keeps the original in-memory ring buffer for tests. New
  `NotificationRepository` (`nox.data.repos`). New `proactive.notification.dismiss {id}` tool,
  persisted like everything else in the store; expired/dismissed rows are pruned by
  `NotificationRepository.purge_expired` (module-level `DEFAULT_NOTIFICATIONS_RETENTION_DAYS = 30`
  default; adding a matching `proactive.notifications_retention_days` to `ProactiveConfig` is
  tracked as follow-up work).
- Settings are writable, not just readable. New `nox.settings` package with
  `config.get`/`config.set` over an explicit allow-list of configuration paths whose types,
  options and bounds are derived from the pydantic models in `nox.core.config`; values are
  validated before anything is written, written to the User layer (`user.yaml`) by the same
  writer `nox onboard` uses, live-applied where that is genuinely possible and honestly reported
  as `restart_required` otherwise. New `settings.changed` event (paths only, never values).
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
- License decided: Apache-2.0. `LICENSE` (full text) and `NOTICE` added,
  `LICENSE-PENDING.md` removed; `pyproject.toml` and both `ui/*/package.json` declare
  `Apache-2.0`. The optional `voice` extra pulls in GPL-3.0 components, so a bundled build
  (e.g. the installer) ships under GPL-3.0 terms as a combined work - stated in `NOTICE` and
  accepted as a documented exception in `docs/license_policy.yaml`.
- Governance and community files for a public repository: `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, root `SECURITY.md`, `.github/CODEOWNERS`, `.github/dependabot.yml`,
  pull-request and issue templates, `docs/PUBLISHING.md`, `scripts/github_branch_protection.ps1`
  and `scripts/check_links.py`.

### Changed
- The core's composition root is a composition root again. `src/nox/app.py` went from 1238 lines
  with a 296-line `start()` to an ordered list of named boot steps; the steps themselves live in
  `nox.core.boot` (`persistence`, `ai`, `workers`, `extensions`), the IPC payload models and
  handlers in `nox.ipc.handlers.core`, and the process entry points in `nox.entrypoints`. Every
  component is declared up front, so shutdown can tell "was never built" from "would not stop" and
  names the component in the log either way.
- The configuration is a package (`nox.core.config`) split by area - installation, security,
  assistant, features, loader - instead of one 1019-line module. Every public name is re-exported,
  so no import site changed.
- Paths below `paths.data_dir` are derived from it. Setting `data_dir` now really does move the
  vault, index, database, cache, backups, runtime directory and logs with it; each can still be
  pinned individually. The `<derived>` placeholder, which contained characters Windows does not
  allow in a path, is gone.
- `security.*` and `ipc.*` settings reach the services that use them as typed models rather than
  as dictionaries, so a renamed key is a type error instead of a silently missing feature.
- Environment references in paths (`${VAR}`, `%VAR%`, `~`) are expanded once, during validation,
  and an unresolvable one is an error naming the variable - never a directory called `${APPDATA}`.
- Configuration layers are validated once each while loading, instead of up to five times.
- `nox onboard` runs in one language. The interface language is the first question and everything
  after it - prompts, hints and the closing summary - follows it. The summary now also says what to
  do next: the start command, where the dashboard is, and that the first vault index takes about a
  minute.
- `nox onboard` recommends connecting Twitch in the dashboard, where the login is a code in the
  browser, and offers typing a token by hand only as the explicit alternative. Both paths store the
  same credential names the Settings page manages.
- `nox doctor` probes the model providers through the egress guard, like the core does, instead of
  opening an unguarded client. Its report goes through one writer, so a caller can capture it.
- `nox --help` and the shipped configuration are written for the person reading them: the internal
  ticket and specification identifiers are gone from `config/defaults.yaml`, `config/profiles/*`
  and the command help, replaced by the constraint each one stood for.

- Every extension (`sensors`, `memory`, `health`, `proactive`, `pm`, `rl`, `clips`, `creative`,
  `remote`) now follows one convention: `install(core)` returns a runtime object with `stop()`,
  which the core holds in its extension registry. Extensions no longer graft attributes onto the
  core object, and the runtimes carry the health checks for what they own.
- `nvidia-smi` is probed in a worker thread with a timeout instead of blocking the event loop for
  up to three seconds on every resource poll, and it is looked up on each call, so a driver
  installed while Nox runs is noticed without a restart.
- Wake-word detection runs in a worker thread over batched frames, and transcription runs one
  utterance at a time, so neither competes with the event loop that carries IPC and heartbeats.
- Output devices are resolved once and cached instead of enumerating PortAudio before every
  utterance; the vault's initial directory walk and the WAV decoding in `stt.transcribe` moved off
  the event loop as well.
- Vector search fetches its mapping rows in one query instead of one per hit, off the loop.
- Shared helpers replace duplicated code: `nox.util.aio` (awaiting a maybe-coroutine, closing an
  async iterator, waiting on a stop event, one poll loop), `nox.util.proc` (process creation flags,
  binary lookup), `nox.worker.heartbeat` (one heartbeat loop for both worker kinds) and
  `nox.plugins.reconnect` (one reconnect backoff with logging for all three transports).
- A worker process exits non-zero after an abnormal end, so the supervisor can tell a requested
  shutdown from a crash, and a worker that cannot measure its own CPU load omits the field instead
  of reporting a perfectly idle process.
- `ProviderInfo` carries a `display_name_de` next to its English `display_name`, so the German
  user interface can show "Regeln (Offline-Notfall)" instead of "Rules (offline fallback)". An
  empty value means the name is the same in both languages (product names are not translated).
- Internal story, spike, requirement and specification identifiers were removed from comments,
  docstrings, manifests and user-visible strings across the voice, worker, plugin, AI, memory,
  sensor, clip, creative, project-management, remote, stream, health and shell packages. Comments
  now state the constraint rather than pointing at a document the reader does not have.
- Voice model and clip directories are derived from the data directory instead of being hard-coded
  absolute paths in the engine modules. **Action required for existing installations whose
  models are not below `paths.data_dir`:** set `voice.models_dir` in `user.yaml` (or move the
  `piper/`, `faster-whisper/` folders), otherwise the engines look under
  `%APPDATA%\Nox\models` and report their models as missing.
- `voice.tts.engine` is validated against `piper|kokoro` instead of being a free-form string.
- `openwakeword` was added to the `voice` extra; clip handling shared by both TTS engines moved to
  `nox.voice.tts.clips`.
- `NOTICE` and `docs/license_policy.yaml` record the honest license status of the Kokoro route:
  Kokoro's own code, ONNX Runtime and the model files are permissive, but `kokoro-onnx`
  still depends on `phonemizer` and espeak-ng (GPL-3.0-or-later), so switching to Kokoro moves the
  GPL obligation on bundled builds rather than removing it. Reaching an Apache-2.0-only installer
  needs a permissively licensed grapheme-to-phoneme front end and remains an open decision.
- `stream.twitch` gained `bot_names`, `relevance_cooldown_s`, `rate_limit_max_messages`,
  `rate_limit_window_s`, `rate_limit_min_gap_s`, `min_backoff_s` and `max_backoff_s` — typed,
  validated, with the manifest's own values as defaults — and all of them are editable on the
  Settings page with de/en labels. The plugin reads them from the configuration
  (`nox_plugin_twitch.settings.resolve_settings`); a manifest that still carries one of the moved
  keys keeps working for one release and logs `twitch.manifest_config_deprecated`.
- Repository prepared for publication: `README.md` rewritten for readers who do not know
  the project, and all personal data (names, e-mail addresses, employer references,
  machine-specific paths) replaced with placeholders across config, plugins, tests, spikes
  and docs.
- Dashboard redesigned for clarity: design system in `styles.css` with light and dark palettes
  (system preference plus System/Hell/Dunkel toggle, `nox.theme`), sidebar navigation, cards,
  status pills, chat bubbles, macOS-Settings-style grouping. ARIA structure, i18n keys and the
  e2e selectors are unchanged.
- Rocket League coach, Stage 1 — observation-only detection, replay pipeline; config scaffolding
  (`rl:` section) landed, feature work ongoing.
- v1.0 public release readiness: license audit, secrets scan, onboarding wizard CLI,
  documentation set, external security review readiness — this changeset.

_No git tags exist yet for the pre-publication history; they will be created at the first tagged
release (`docs/PUBLISHING.md`) - until then the compare links below point at tags that don't exist
yet either._

### Fixed
- **CI has been failing since 2026-09-28, and not for any of the reasons the open dependency PRs
  were blamed for.** Every one of #45-#62 died the same way: `Windows fatal exception: access
  violation` in `security/audit.py`, from the queued audit writer's thread. The writer shares the
  core's SQLite connection under its own lock, and both `NoxCore.stop()` and a unit fixture closed
  that connection while the thread was inside a statement - which `sqlite3` does not raise on, it
  faults. Both are fixed; the suite now runs to completion in CI (1982 passed) instead of dying at
  78%.
- **A supervisor test asserted on a process record the instant the process disappeared**, which are
  two different moments. It passed locally and failed in CI - the same shape as the restart test
  beside it, and fixed the same way.
- **The Meereswolf no longer has a black outline on a light desktop.** Its cut-out had been stored
  with premultiplied colour under a straight-alpha flag, which left every soft fur edge too dark by
  its own alpha - mean border luminance 0.102 against fur at 0.352. The matte is now repaired
  reproducibly (measure the background off the transparent pixels, erode two pixels, divide the
  colour back out, re-feather, lift what a second background grey left behind) and the border sits
  at 0.378, matching the fur it belongs to. The creature also carries a contact shadow in light
  mode, because a white wolf on a white desktop is only 2.7:1 whatever the edge does - that part
  needs a new source image, and `ui/pet/scripts/README.md` states what one has to provide.
- **Eleven of the Meereswolf's twelve frames were the same photograph.** `speaking-0` was
  byte-identical to `idle`, `thinking-0` to `thinking-2`, and `blink` was the idle frame dimmed by
  3 %, so the pet's "blink" read as a monitor flicker and its "sleeping" had both eyes wide open.
  They are gone, and the manifest now advertises only the one drawing that exists; every state it
  used to claim is produced by the rig instead.
- `security.pin_required_for_security_changes` does something. It was declared in the configuration
  and read by no code at all. With a PIN configured, a change that *relaxes* protection now has to
  carry it: leaving a stricter privacy mode, switching a capture device back on, or editing a
  `security.*` / `privacy.*` setting from the dashboard. Making Nox stricter never asks, so a
  forgotten PIN can never trap it in an open state.
- A broken audit chain no longer boots into normal operation. Verification is checked properly - the
  result used to be read with a default of "fine" - and a failure engages the kill switch, so Nox
  starts in safe mode, denies every action with a side effect, says so on screen, and needs the PIN
  to leave. Boot verifies forward from the last verified position instead of rescanning years of
  entries.
- A plugin can no longer take over the voice service. `worker.register` and `worker.ready` are
  restricted to spawned workers, and a worker may only claim the service its one-time token was
  issued for. Plugins also no longer receive raw transcripts, model responses, memory events or
  chat text over the event stream - the event boundary now matches the state boundary that was
  already in place.
- Only the process the supervisor actually spawned can claim the `core` role or send heartbeats.
  The control token is shared with the shell, so any holder of it could previously become the core
  connection, intercept the kill switch and keep the watchdog quiet with forged heartbeats. An
  unauthenticated connection now also has an authentication timeout and a frame budget.
- Permission checks and outbound requests no longer write to the database on the event loop. Audit
  entries are handed to one writer thread that preserves their order; nothing is dropped, and a
  full queue makes the caller write its own entry rather than losing it.
- The dashboard's stream page could never load. `stream.session.status` and `stream.funken.top`
  were registered for the UI roles but missing from a second, parallel allow-list that had to agree
  - so every call was refused before the handler ran. A handler's own roles are the authority now.
- A profile rule scoped to a directory is no longer bypassed by `..` in the target, and a temporary
  grant for project `a` no longer covers every name starting with `a`.
- A PIN lockout survives a restart. The counter lived in memory only, so restarting the core - which
  the supervisor does on request - reset it after five attempts.
- When Argon2 is unavailable, Nox says so at startup and in the PIN error instead of reporting
  "wrong PIN" for a PIN that may well be right.
- `[::1]` and other IPv6 entries in an allow-list are parsed correctly instead of being read as host
  `::` port `1` and silently matching nothing. An unparsable entry is now rejected when the
  configuration is read.
- Nested values in an audit entry are redacted at every level; a secret one level down used to be
  stored verbatim.
- `GET /health` no longer discloses the vault path to unauthenticated local processes, and reports
  whether the session token file could be restricted to the current account.
- The kill switch is audited before its stop hooks run, so a failing hook cannot cost the record
  that the kill happened; the report says when it is incomplete.
- `nox voice` says why it is unavailable instead of vanishing from the command list, and a failing
  Twitch token refresh is logged and reported as limited instead of retrying silently forever.
- `nox rl calibrate` stores its calibration with the rest of Nox's data instead of in a fifth,
  hard-coded location under the home directory.
- The supervisor no longer blocks its own event loop for up to two seconds while terminating a
  process tree, and no longer raises when a process exits while it is being killed.
- A client can no longer grow the hub's inbound event queue without limit, a request that fails
  inside the hub is always answered instead of leaving the caller to time out, and the hub keeps a
  reference to its own background tasks.

- Screenshot capture in creative apps no longer proceeds when the active security profile cannot
  be read: an unreadable security engine is refused, with the reason, instead of being treated as
  "the Work profile is not active". The same rule now applies to the creative-app mode switch.
- Memory embeddings are only requested through the egress guard. Without a guard, the embedding
  provider is not built at all and the `memory.embeddings` health check reports `unavailable` with
  the reason; note text never leaves the process unguarded, and search falls back to full-text.
- A `/privacy` command from a paired phone now reports what actually happened: a privacy switch
  that failed answers "Privatsphäre-Modus nicht geändert" instead of a mode the system never
  entered.
- A failed security-profile switch on entering or leaving Rocket League mode is logged and named
  in the `system.mode_changed` reason ("security profile unchanged") instead of being swallowed
  while the mode change claims the game profile is in force.
- `rl.callout` is published only for a callout the user actually heard. A missing or failing
  speaker is logged; it no longer produces a transparency entry with a measured latency for audio
  nobody played.
- `listening_mode: ptt_only` now keeps the capture device closed until push-to-talk is held, and
  closes it again on release, instead of holding an open microphone stream (and the operating
  system's microphone indicator) for the whole session and discarding the frames.
- A failing microphone capture loop is reported: the voice pipeline's new capture health check
  says `unavailable` with the reason instead of leaving the pipeline permanently deaf while it
  still reports itself as running.
- Every `tts.speak` ends in exactly one terminal event. A playback error outside the expected set,
  and a say-task that dies in the worker, now emit `tts.finished {ok: false, reason}` instead of
  leaving the caller waiting forever.
- Barge-in is no longer lost in the moment between `tts.started` and playback, and a `tts.stop`
  that arrives while an utterance is still queued now cancels that utterance instead of being
  cleared when it finally starts.
- A plugin secret is refused when the access cannot be written to the audit chain, instead of
  being handed out with no trace.
- A plugin worker no longer inherits the core's entire environment; it gets a minimal, explicit
  set (interpreter, home and temp directories, and the two `NOX_*` variables).
- A plugin's own `src` directory is appended to `sys.path` instead of prepended, so a plugin
  shipping `src/asyncio.py` or `src/nox/` can no longer shadow the standard library or the core.
- The Rocket League plugin's kill-switch handler closes the capture gate and requires an explicit
  resume; cancelling the capture loops alone let the next `game.detected` restart capture without
  any new consent.
- A failed coding-session run keeps its captured stderr instead of cancelling the reader, and a
  CLI process that survives `kill()` is logged with its pid rather than leaked silently.
- The OBS, Twitch and Telegram clients log connection failures (the first of a streak and every
  attempt at the backoff ceiling). A wrong password no longer retries forever in complete silence.
- The Telegram client no longer chains the original httpx exception, whose message carries the bot
  token in the request URL, into the error it raises.
- The AI router's fallback event names the providers left *after* the failed one, instead of
  offering the provider that just failed as its own fallback.
- The degraded-mode service ignores its own `system.level` announcement instead of relying on a
  constructor argument not to list it.
- A note written while the first vault scan is still running is no longer deleted from the index
  again: the scan's pruning pass now only removes entries the index already held when it started.
- A sensor whose poll raises keeps polling and reports itself as `limited` through the new
  `sensors` health check, instead of ending its task in silence while the status tool serves a
  frozen sample as if it were live.
- `nox.settings.layers.write_user_config` — the one writer behind `config.set` and `nox onboard` —
  round-trips the User layer through `ruamel.yaml` instead of `yaml.safe_dump`: comments, key
  order and quoting style survive a settings change, and only the keys in the patch change. What
  is written stays plain YAML; every reader still parses it with `yaml.safe_load`. A `user.yaml`
  that does not parse now raises `UserConfigError` and is left untouched instead of being quietly
  replaced by a fresh document. New core dependency `ruamel.yaml` (MIT).
- The Stage 1 HUD `_recognize_loop` (`plugins/rl/src/nox_plugin_rl/plugin.py`) captured the
  screen on a timer regardless of privacy state, unlike Stage 2's `_vision_loop`, which already
  stops while a privacy zone is active or `privacy.capture.screen` is off. New
  `nox.rl.capture_gate.CaptureGate`: a `privacy.capture_changed`-driven gate (reusing
  `PrivacyService.effective_capture()`'s `screen` boolean - already accounts for
  zone/mode/capture-flag/panic/safe-mode) meant to back both loops so neither hand-rolls its own
  privacy check; logs `rl.capture_paused reason=...` / `rl.capture_resumed` exactly once per
  transition and guarantees a paused loop grabs no frame at all via `maybe_capture()`.
- `ProactiveService.__init__` used `store or NotificationStore(...)`; since `NotificationStore`
  defines `__len__`, a caller-supplied store that was still empty (0 records) was falsy and got
  replaced with a fresh, unrelated in-memory store - notably `nox.proactive.install.install`'s
  now-db-backed store right after boot. Fixed to an explicit `is not None` check.
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
- Core froze after `core.started`: the memory vault scan blocked the event loop — one fresh SSL
  context (~250 ms of CA-bundle loading) per embedding request, and no yield at all on already
  indexed notes. Heartbeats stopped, the supervisor restart-looped the core into safe mode.
  Now: one shared SSL context per process (`nox.security.egress.shared_ssl_context`), the
  memory extension's Ollama client goes through the egress guard like the chat provider,
  and the indexer yields after every chunk and note.
- Voice worker died on a single 5 s handshake timeout while the core was still booting; it now
  retries with backoff for up to 60 s.
- Shell stayed offline for the whole session once the first bridge connect failed, and could
  not follow a supervisor-restarted core (stale session token). It now re-reads the runtime
  files, retries every 3 s, drops a bridge after two failed pings, and reloads the pet page.
- `pm` extension refused to start because `08 - Epics/Epics Overview.md` (a map note) was
  parsed as a work item; only files matching the project's own work-item naming pattern are
  parsed as work items now.
- `nox doctor` warns when the venv runs on Microsoft-Store Python (virtualized `%APPDATA%`,
  the cause of `token_acl_failed`).
- First voice turn deadlocked the worker connection (hub delivered the worker's event inline,
  the orchestrator awaited LLM + TTS inside the handler, TTS waited for `tts.finished` from the
  blocked socket): voice turns run as a task, the hub pumps inbound events per client off the
  receive loop, the worker reconnects and re-registers, the core forgets a dropped worker, the
  shell reloads the pet page when the session token changed.

## [0.2.0] - 2026-09-14

Stream companion: Twitch chat, OBS scene control, and the Funken viewer-loyalty system, on top of
the v0.1 walking skeleton.

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

### Security
- Graceful supervisor shutdown, speech gate (no unsolicited speech in private modes), typed chat
  stream, pet variant scaffolding.

## [0.0.0] - 2026-09-09

Repository foundation: package layout, `config/defaults.yaml`, and the interface contracts other
modules build against (`events.py`, `state.py`, `model.py`, `protocol.py`, `base.py`). No running
product yet.

No tags exist yet: the links below used to point at commit hashes from the pre-publication
history, which 404 on the public repository (#29) - they now use the tag-based form
`docs/PUBLISHING.md`'s squashed-snapshot publish is expected to create tags for
(item 16 of `docs/RELEASE_CHECKLIST.md`), even though those tags do not exist yet either; both
`v0.1.0` and `v0.2.0` get an actual release tag at first publish, `v0.0.0` ("Repository
foundation") predates the tagging process and never gets one, hence the single-tag link shape
below instead of a compare for it and for `v0.1.0` (nothing tagged to compare either against).

[Unreleased]: https://github.com/Crackxsy/nox/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/Crackxsy/nox/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Crackxsy/nox/releases/tag/v0.3.0
[0.2.0]: https://github.com/Crackxsy/nox/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Crackxsy/nox/releases/tag/v0.1.0
[0.0.0]: https://github.com/Crackxsy/nox/releases/tag/v0.0.0
