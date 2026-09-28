> **Provenance.** Machine-assisted subsystem audit of the tree between `6625b50` and `3af0a5a` (read-only, code-reading
> with a few scratch experiments), written for [`../CAPABILITY_ANALYSIS.md`](../CAPABILITY_ANALYSIS.md).
> Line numbers may be off by a few lines for files changed since. Findings that the consolidated analysis relies on were re-checked
> by hand and are marked there; treat everything else here as a well-sourced lead, not a verdict.

# Audit E: Plugin runtime and domain features (Nox @ 6625b50)

Scope: `src/nox/plugins/`, `src/nox/worker/plugin.py`, all 9 plugins under `plugins/`, the domain extensions `src/nox/{home,stream,rl,clips,creative,remote}` (plus `pm` where it touches plugins), `docs/PLUGIN_AUTHORING.md`, and the related tests. This was a read-only audit: no repo files were changed and the test suite was not run. Every claim below comes from reading the code at the cited file:line.

The most important cross-cutting fact comes first, because it changes the maturity rating of almost every tool below.

> **No model-driven tool calling exists.** `src/nox/core/orchestrator.py` (399 lines) contains no tool references at all. `ToolExecutor.call` has exactly six call sites: `clips/service.py:80`, `clips/ipc.py:29-53`, `home/ipc.py:110`, `remote/install.py:102`, `stream/responder.py:112` and `stream/booking.py:111`. The dashboard's IPC surface only covers `home.*`, `clip.*`, `remote.*`, `stream.*`, `twitch.auth.*` and `plugin.status`.
> **As a result, 21 of the 36 plugin tools can never be invoked by a user or by the assistant.** These are every `coding.*`, `rl.*` (6), `creative.*` (2), `obs.status.read`, `obs.scenes.list`, `obs.scene.switch`, `obs.privacy_scene.activate`, `obs.preflight.check`, `obs.replay_buffer.status.read`, `twitch.chat.status.read`, `telegram.status.read` and `echo.ping`. The only exceptions are tests and the RL `nox rl calibrate` CLI (`cli.py:111-120`), which bypasses the tool entirely.

---

## 1. Plugin runtime

### 1.1 Lifecycle (`src/nox/plugins/manager.py`)

| Step | Where | Notes |
|---|---|---|
| discover → validated | `discover()` 493-515 | `load_manifest` plus `check_egress` against the active profile. A bad manifest goes to `FAILED` and is isolated. |
| enabled | `_apply_enablement` 629-645 | Requires the id to be in `plugins.enabled` **and** `matches_profile`. Default `plugins.enabled: []` (`config/defaults.yaml:166`). |
| spawn | `_spawn` 655-679 | `python -m nox.worker --plugin <id>`, filtered env (183-219), a one-time token (`tokens.worker_env`), `NOX_HUB_URL` and `NOX_PLUGINS_DIR`. Job object on Windows (`job.assign`); `parent_env()` elsewhere. |
| handshake | worker `run()` `worker/plugin.py:133-164` | connect → subscribe(manifest listens + `security.*`, `privacy.*`, `system.stopping`) → `create(api)` → `plugin.register` → heartbeat → `start()` |
| registration | `_h_register` 798-824 | Checks the plugin id matches the connection and the state is SPAWNED/REGISTERED/RUNNING. `declare_services(event_namespaces)`, then each tool is re-validated against the manifest (name, namespace, **exact risk**) and mirrored into the shared `ToolRegistry` with a passthrough `input_model` (256-274). |
| crash → restart | `_monitor` 681-694 (polls `proc.poll()` every 0.5 s) → `_on_crash` 696-725 | Backoff `2 s * 2^(n-1)` (2 s, 4 s, 8 s). After 3 restarts inside 300 s the plugin goes permanently `FAILED` (351-354). |
| stop | `_stop_plugin` 727-748 | Sends `plugin.stop` with a 2 s ack window, then `terminate`, then `kill` after 2 s. |
| kill switch | `app.py:447` hook → `stop_all("kill_switch", ack_timeout_s=1.5)` (`app.py:647-649`) | The worker also stops itself on the `security.kill_switch` event (`worker/plugin.py:114-116`). |
| profile switch | `_on_mode_changed` → `apply_profile` 543-579 | Spawns newly matching plugins and stops ones that no longer match. Skips `FAILED` records (555). |

Manifest schema (`manifest.py:89-171`, `extra="forbid"`):
- `id` must match `^[a-z][a-z0-9_]{1,31}$` and the directory name.
- `api_version` must be in {1}.
- `entry` must have the form `pkg.mod:callable`.
- `profiles` (empty means every profile) and `permissions[{tool, risk=MEDIUM default}]`. Tools must be in the `<id>.` namespace, dotted, and not hard-prohibited.
- `events.{emits,listens}` must be dotted names.
- `secrets` must be `nox/<id>/…`.
- `network.egress` entries must be `host:port`.
- `resources{memory_mb, priority}`: **validated but never enforced.** Only `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is set (`core/jobobject.py:104`), and priority is never applied.
- `config` (free dict, passed as `api.config`).

### 1.2 What isolation is actually enforced

| Boundary | Enforced? | Evidence |
|---|---|---|
| Separate process | Yes | `_default_process_factory` 647-653 |
| Environment | Filtered allow-list, but `PYTHONPATH`/`PYTHONHOME` are passed through | 183-219 |
| `sys.path` | Plugin `src/` is **appended**, so it cannot shadow stdlib or `nox` | `worker/plugin.py:39-57`. The plugin can still `import nox.*` freely, and every first-party plugin imports core internals (`nox.rl.replay_parser`, `nox.home.boundary`, `nox.settings.layers`, `nox.ai.providers.claude_code`, …). |
| Egress via `api.http()` | Yes, `PluginEgressGuard` scoped to the manifest plus privacy mode | `api.py:85-135`, `331-337` |
| Egress via raw sockets / websockets | **Voluntary only.** OBS/Home/Twitch call `api.egress.authorize()` themselves before connecting (`obs/plugin.py:109-114`, `home/plugin.py:99-114`, `twitch/plugin.py:94`) | Nothing stops a plugin from calling `asyncio.open_connection` directly. The coding plugin's `claude` subprocess has unrestricted network access (§3). |
| Events emitted | Namespace-gated at the hub (`server.py:758-771`) | The granted namespaces come from the manifest's **own** `emits` (`manifest.py:124-131`) and **no namespace is reserved**. A plugin that declares `emits: [remote.message]` or `[game.detected]` can inject phone commands (RemoteService does no source check, `remote/service.py:124-130`) or switch the security profile (`rl/services.py:68-85`). |
| Events received | Hub filters by role only (`server.py:84-94`) | `_subscribe` accepts any pattern (`server.py:745-756`). The manifest `listens` list is enforced only in-process by `PluginApi.events.on` (`api.py:247-259`). Any plugin can subscribe to `remote.message` (phone chat text), `sensor.foreground_changed` (window titles), `home.*` and `coding.*`. |
| Requests | Role allow-list `ipc.*`, `plugin.**`, `state.get`, `worker.heartbeat` (`dispatch.py:92`), plus the declared namespaces as `<ns>.**` (`dispatch.py:100-108`) | Every core handler I found declares `roles=`, so this is safe today. |
| Cross-plugin / core tool calls | **Possible** via the `plugin.tool.call` handler (`manager.py:919-946`) | Contradicts the claim in `clips/manifest.yaml:3-9` that there is "no cross-plugin tool call". The handler performs a permission check only (`agent="plugin"`, with `target` taken from caller input, 932). There is **no audit, no kill-switch check and no input validation** (it calls `spec.handler` directly, 945). Example: any plugin can call `home.light` (companion rule `allow`) or `telegram.send` with any `chat_id`. |
| Secrets | Name-gated, audited, refused if the audit write fails | 870-917 |
| State reads | `PUBLIC_STATE_ROOTS = assistant, privacy, system` | `handlers/core.py:196-208` |
| Config | Twitch/Home workers read the **whole merged user config from disk** (`twitch/settings.py:309-317`, `home/settings.py:32-40`) | This goes beyond the manifest `config` block. |

### 1.3 Tool-call flow and timeouts

UI or core service → `ToolExecutor.call` (input validation against the passthrough model, permission check, confirm wait up to 60 s, **kill-switch deny** at `executor.py:177-190`, 30 s run timeout at 28, cancelled on a kill event) → forwarding handler (`manager.py:852-868`, `hub.request(... timeout=tool_timeout_s=30)`) → worker `_on_tool_call` (`worker/plugin.py:92-97`) → `PluginToolsApi.call` (the pydantic validation that actually matters, `api.py:218-228`) → plugin handler.

There is **no worker-side timeout or cancellation.** When the core times out after 30 s, the handler keeps running in the worker and its result is dropped. The inbound request runs as a spawned task (`ipc/client.py:369-370`).

**Unused setting:** `register_timeout_s` (358) is never read. A worker that hangs before `plugin.register` stays `SPAWNED` forever, and health reports `LIMITED` rather than `FAILED`.

### 1.4 Health

`health_of` (583-597) is process-state only: RUNNING means "available (N tools)". **Every plugin's own `health()` method (`TwitchPlugin.health`, `ObsPlugin.health`, `HomePlugin.health`, `TelegramPlugin.health`, `RlPlugin.health`) is dead code.** Only unit tests call them (verified by grep). The system health view therefore says Twitch is AVAILABLE even when it has no token and no connection.

### 1.5 Worker ↔ hub connection

The plugin `IpcClient` is built without `reconnect=True` (`worker/plugin.py:203-210`, default False at `ipc/client.py:142`). When the connection drops, `_read_loop` just nulls the connection (`client.py:340-350`). The worker process stays alive, the heartbeat logs a warning every 2 s forever (`heartbeat.py:47-50`), and the manager still sees a live PID and reports RUNNING/AVAILABLE. `app.py:717-720` only detaches *workers* on `IPC_CLIENT_DISCONNECTED`, not plugins. Result: a **zombie plugin** (see R2).

---

## 2. Capability table

Maturity key: **WORKS+TESTED**, **WORKS-UNPROVEN-IN-REAL-ENV** (only fakes ever exercised), **LIMITED**, **SCAFFOLDING**, **MISSING**. "Unreachable" means no production caller exists (see the top-of-report note).

### 2.1 Plugins

| Plugin (profiles / egress) | Tools (risk, side effect, confirm?) | What the user can actually do | Maturity | Tests | Key gaps |
|---|---|---|---|---|---|
| **echo** (all / none) | `echo.ping` (read, no side effect) | Nothing; reference plugin | WORKS+TESTED | `unit/plugins/test_*`, `integration/test_plugins.py` (5) | none |
| **twitch** (`stream` / `irc.chat.twitch.tv:6697`, `api.twitch.tv:443` unused) | `twitch.chat.send` (low, side effect, allow in stream `stream.yaml:55-58`); `twitch.chat.status.read` (read, unreachable) | Bot joins the channel via IRC over TLS. Chat is forwarded as `twitch.chat_message`. Built-in `!rps <stein\|schere\|papier>`, `!help`/`!hilfe`, `!funken` (answered core-side, `booking.py:101-117`). Nox's LLM answers "addressed" chat lines (`responder.py`). OAuth device-code login plus refresh loop core-side (`settings/twitch_auth.py`, `settings/install.py:249-270`). | WORKS-UNPROVEN-IN-REAL-ENV | 71 unit + 3 integration (fake IRC) | No handling of USERNOTICE (subs/raids/bits), RECONNECT, CLEARCHAT or `msg_*` NOTICE failures. No moderation actions (by design). `sent: True` is returned without server confirmation (`plugin.py:279-280`). No CR/LF sanitising (R9). Backoff never resets (R4). Every `!command` triggers an LLM reply as well (R10). Viewers can drain the reply budget (R11). |
| **Funken economy** (core `stream/funken.py`, `booking.py`) | none (docstring promises a `twitch.funken.*` tool, `funken.py:8-9`, which does **not exist**) | Earn 5 Funken per `!rps` win (`twitch/plugin.py:245-249`), capped at 200/day per viewer (in memory, resets on restart, `booking.py:50,86-99`), and only while an OBS-derived stream session is active. `!funken` reports the balance. Leaderboard at `stream.funken.top`. | LIMITED | `unit/stream` (27), `integration/test_stream_core.py` (2) | Earn rates for message/sub/bit/raid (`defaults.yaml:172-176`) are dead config: nothing calls `earn` with those reasons. No spend or redemption, no admin-adjust surface. The `!funken` reply has no @mention, so the viewer can't tell whose balance it is. |
| **obs** (`stream` / `127.0.0.1:4455`) | `obs.status.read`, `obs.scenes.list`, `obs.preflight.check`, `obs.replay_buffer.status.read` (read, all unreachable); `obs.scene.switch`, `obs.privacy_scene.activate` (medium, confirm, unreachable); `obs.replay_buffer.save` (medium, **default confirm**, reached only through the clips service) | Emits `obs.connected/disconnected/scene_changed/recording_changed` and `stream.started/ended`, which drive stream sessions and Funken gating. The panic → privacy-scene switch is meant to be the safety feature. | WORKS-UNPROVEN-IN-REAL-ENV (a `@pytest.mark.network` real test exists but is skipped, `integration/test_obs_plugin.py:177-182`) | 33 unit (fake OBS) + 3 integration | **The panic → privacy scene never runs in the real flow** (R1). No state resync on connect (R6). Scene set and privacy scene are "pending approval ES-02" (`manifest.yaml:47-57`). |
| **home** v3 (`companion` / `127.0.0.1:8123`, `homeassistant.local:8123`) | `home.status.read`, `home.list`, `home.state` (read); `home.light`, `home.switch`, `home.scene`, `home.media`, `home.cover` (medium, **allow** in companion, `companion.yaml:48-72`); `home.climate` (medium, confirm); `home.script`, `home.automation.trigger` (high, confirm) | Dashboard Home page: list, toggle, scene, and typed German/English commands through deterministic intent matching (`home.command`, `home/ipc.py:134-194`). Intent matching is **only** reachable from dashboard/shell text. There is no voice or LLM path, since `resolve_intent` has no caller outside `nox.home`. | WORKS-UNPROVEN-IN-REAL-ENV (network test skipped, `integration/test_home_plugin.py:283-292`) | 41 plugin unit + 57 core unit + 7 integration (fake HA) | Real-environment risks: 1 MiB websocket frame cap (R5); egress only covers loopback and `homeassistant.local` (R14); live view is empty until the first tool call (R7); client never reconnects after a panic (R8); scenes and switches bypass the hard boundary (R12); climate is Celsius-only (`models.py:93`), which is wrong for a °F installation. |
| **telegram** (`companion` / `api.telegram.org:443`) | `telegram.send` (low, allow, **arbitrary `chat_id`**); `telegram.status.read` (read, unreachable) | With `remote.enabled: true` (default false): pair a phone with an 8-character code, then `/status`, `/kill`, `/privacy private\|offline`, `/unpair`, and free-text chat through the orchestrator (`remote/service.py:1-17`). Outbound notifications go to an allow-listed set of events. | WORKS-UNPROVEN-IN-REAL-ENV | 20 unit + 62 remote unit + 13 integration (fake Bot API) | **Notifications and replies can go to an unpaired stranger** (R3). **Replies and notifications are denied while the kill switch is engaged**, so `/kill` never gets its confirmation and the "critical" notifications never send (R1b). The poll loop can die silently (R13). |
| **coding** (`coding` / none, but the CLI has its own network access) | `coding.session.start` (medium), `coding.session.status.read` (read), `coding.session.stop` (low), `coding.review.request` (medium; template text, no AI) | **Nothing: all four tools are unreachable** (no IPC, no UI, no model tool calling; grep finds no caller). | Runner is WORKS-UNPROVEN (fake CLI `tests/unit/plugins/coding/fake_claude.py`); **feature as a whole is unreachable** | 16 unit + 3 integration | End to end: no. `session.start` blocks for the whole session, so the core times out at 30 s and never receives the `session_id` (R15). `session_timeout_s` is never read (only in `manifest.yaml:45`). Workspace root is a placeholder `%USERPROFILE%/Projects` with no env expansion, which fails closed. Confinement = cwd root check (`plugin.py:72-88`) plus Claude CLI `--tools Read,Edit,Write,Glob,Grep --permission-mode acceptEdits --add-dir` (`session.py:134-170`); Nox does not enforce file access itself. Privacy mode is ignored, so code goes to Anthropic even in OFFLINE. stderr pipe drain issue (R16). |
| **rl** (all / none) | `rl.status.read`, `rl.replay.list`, `rl.replay.summary`, `rl.vision.status.read` (read), `rl.calibrate`, `rl.vision.enable` (medium). All unreachable; calibration is available through the CLI. | Detects `RocketLeague.exe` via psutil and auto-switches the mode and security profile (`rl/services.py:68-101`). Parses new `.replay` headers into the DB (`nox/rl/replay_parser.py`, stated as validated on real replays). Post-session template or AI summary. | **Stage 1:** process detection and replay header parsing WORK. HUD recognition is **SCAFFOLDING**: synthetic Hershey-font digit templates, never matched to the real HUD font (`recognizers.py:1-13,28-40`), and "HUD present" is just `np.std(frame) > 3` (`calibration.py:67-72`), true for almost any screen. Callouts are **MISSING in practice**: clip ids such as `careful_demo` (`callouts.py:60-89`) have no `.wav` in the repo and no generator, and TTS does not fall back to `fallback_text` (`piper_engine.py:127-130`, `clips.py:57-60`). **Stage 2:** off by default. The OpenCV HSV heuristic is uncalibrated ("real false-positive risk", `opencv_backend.py:1-8`), the ONNX backend is a stub (`onnx_stub.py:30-43`), and the output is a post-match "Rough vision read (experimental)" line (`rl/vision.py:180-181`). **No real coaching output today; no ML model; no real HUD data.** | 27 plugin unit + 47 core unit + 3 + 5 integration | `rl.match_ended.duration_s` is hard-coded 0.0 (`plugin.py:294`). Primary monitor only (`calibration.py:138-141`). A new `mss.mss()` plus a full-frame numpy copy for every capture at 2 Hz + 1 Hz (no leak, since a context manager is used, but high cost at 4K). psutil process scan runs synchronously on the loop every 2 s (`plugin.py:165`). The mode bridge breaks streaming (R2b). |
| **clips** (stream, coding, companion / none) | none (core registers `clip.list/tag/export/trim` on the shared registry, `clips/tools.py:206-209`, and IPC at `clips/ipc.py:61-64`) | Dashboard clip library: list, tag, export, and trim when `ffmpeg` is on PATH (`trim.py:1-4`). Auto clip on a `rl.event` goal or on `!clip` from any viewer → `obs.replay_buffer.save` → ingest/checksum/index. | Library WORKS+TESTED; auto-capture **LIMITED** | 16 plugin unit + 35 core unit + 1 integration | `twitch.chat_mood_changed` is never emitted by anyone (only declared at `events.py:134`), so the chat-hype trigger is dead. `obs.replay_buffer.save` is medium risk with no stream rule, so **every auto clip needs a manual confirm within 60 s** (`permissions.py:59-65`; no `replay_buffer` rule in `config/`). Any viewer can spam `!clip` (one save per 15 s, unbounded disk). The service hard-codes `mode="stream"` (`service.py:84`). The clips plugin stops in `rocket_league` mode (R2b). |
| **creative** (all / none; not enabled by default) | `creative.artifact.inspect` (low; **any path on disk**, `plugin.py:172-174`); `creative.screenshot.analyze` (medium). Both unreachable. | Foreground-app hysteresis sets `assistant.mode` to creative (`creative/service.py:105-112`). | LIMITED (detection) / SCAFFOLDING ("analyze") | 29 plugin unit + 18 core unit + 5 integration | "analyze" only saves a PNG; there is **no analysis** anywhere. The capture is the **whole primary monitor**, not the app window (`creative/screenshot.py:159-170`), so the privacy-zone check on the app title does not protect other visible windows. `.blend` inspection is header only (`artifact.py:260-275`). DaVinci and browser DAW patterns are marked speculative (`manifest.yaml:47-52`). |

### 2.2 Domain extensions (core side)

| Extension | Surface | Maturity | Notes |
|---|---|---|---|
| `nox.home` (boundary, intent, targets, lexicon, probe, ipc) | 7 IPC requests for UI roles (`home/ipc.py:137-143`) | Intent matcher WORKS+TESTED (pure). The feature depends on the unproven plugin. | Boundary checks entity domain and cover device class only (`boundary.py:104-125`). |
| `nox.stream` (sessions, responder, funken, booking) | `stream.session.status`, `stream.funken.top` | WORKS-UNPROVEN | Sessions open only on OBS `StreamStateChanged` (R6). The responder has no tools, so prompt injection is limited to what Nox says in chat. The moderation gate is a keyword list (`twitch/moderation.py:16-91`) and is easy to evade. |
| `nox.rl` (services, vision, replay_parser, capture_gate) | health checks, DB | Replay parser WORKS. Everything else depends on the scaffolding HUD stage. | The core imports plugin code into the core process (`rl/install.py:115-125`). `capture_gate.py:28-31` carries a stale "open point" (the recognize loop already uses the gate; the vision loop still does not, `rl/plugin.py:311-313`). |
| `nox.clips` | clip IPC and tools, watcher, ingest | WORKS+TESTED (library) | ffmpeg and opencv are optional and reported honestly. |
| `nox.creative` | mode service, screenshot service | LIMITED | see the creative row |
| `nox.remote` | pairing, policy, service, notifier | WORKS-UNPROVEN, with R3 and R1b | Pairing is solid: 40-bit single-use code, 5-minute TTL, only a salted hash stored, atomic redeem. The replay check is skipped when `update_id == 0` (`policy.py:115`, falsy check). |
| `nox.pm` | Not touched by any plugin: `coding.session_*` events have **no consumer** anywhere in `src/` | n/a | Coding sessions are not linked to PM stories despite the `story_id`/`project_id` inputs (`coding/plugin.py:54-55`, which are then emitted as `""`, 146-147). |

---

## 3. Reliability, security and privacy findings (severity, file:line, concrete scenario)

**R1 – HIGH – Panic never switches OBS to the privacy scene.**
`KillSwitchService.panic` (`security/killswitch.py:239-250`) first calls `engage()`, which publishes `security.kill_switch` (137-143) and runs the stop hooks, including `plugins.stop` (`app.py:447,647-649`). Only after that does it publish `security.panic`. The OBS worker stops itself on the kill event (`worker/plugin.py:114-116`) and is then terminated, so `ObsPlugin._on_panic` (`obs/plugin.py:199-211`) has no live process left to run in.
Scenario: the streamer hits panic while live, and the stream keeps showing the live scene. `tests/unit/plugins/obs/test_panic.py:27-40` fires the event straight into an in-process plugin, so the test passes while the real flow is broken.

**R1b – HIGH – Every phone reply and "critical" notification is refused during the kill switch.**
`engage()` sets `_engaged=True` (123) before publishing. `RemoteNotifier` and `RemoteService._reply` send through `ToolExecutor`, which denies every call while the switch is engaged (`executor.py:177-190`). The telegram worker is being stopped at the same time.
Scenario: the user sends `/kill` from the phone. The kill happens, but the reply "Not-Aus aktiv" (`remote/service.py:180-185`) and the notifier's `security.kill_switch`/`security.panic` messages are denied. `CRITICAL_EVENTS` bypassing quiet hours (`notify.py:33`) is effectively dead code.

**R1c – MEDIUM – Nothing restarts plugins after a kill-switch resume.**
`resume()` (`killswitch.py:194-236`) publishes no event that the plugin manager consumes, and `apply_profile` runs only on `system.mode_changed` (487, 543-544). There is no manual restart request either.
Scenario: after resuming, Twitch, OBS, Home and Telegram all stay `STOPPED` until the next mode switch or a full Nox restart. The phone channel stays dead after a remote `/kill`.

**R2 – HIGH – Zombie plugins after the hub drops the connection.**
The hub closes a client on rate limit (50/s, burst 200, `server.py:124-125,634-642`) or on event backlog (1000, `server.py:130,669-676`). The plugin `IpcClient` has no reconnect (§1.5). The process stays up, and `health_of` reports AVAILABLE.
Scenario: a raid of 200+ viewers spamming `!rps stein`. Each message emits 2-5 frames (`chat_message`, `command_invoked`, `minigame_started/ended`, `funken_awarded`), which trips the bucket. The Twitch plugin is disconnected from the core and chat goes silent, while the dashboard shows it running. Next, the IRC loop tries to emit on a dead client, `_on_disconnected` raises outside the `try` (`irc_client.py:136`), and the reconnect task dies. The same unguarded pattern exists in `home/ws_client.py:159` and `obs/ws_client.py:163`.
The home plugin rate-limits state events to 10/s (`home/plugin.py:168-178`), so HA floods are covered. Twitch has no such limit.

**R2b – HIGH – Launching Rocket League shuts down the stream stack, and closing it leaves the wrong profile.**
`RlModeBridge._on_detected` unconditionally sets mode and profile to `rocket_league`, even from `stream` (`rl/services.py:68-85`). `apply_profile` then stops every plugin whose `profiles` omits `rocket_league`: twitch and obs (`[stream]`), clips (`[stream, coding, companion]`), home and telegram (`[companion]`).
On `game.ended`, the bridge restores the *mode* to the previous one but sets the *security profile* to `"companion"` (`services.py:99`, `_mode_reason("game.ended","companion")`).
Scenario: the streamer launches RL while live. The Twitch bot, OBS link and clip detector stop mid-stream. After quitting RL, the UI shows mode `stream`, but the profile is `companion`, so Twitch and OBS never come back. The flagship "RL goal → auto-clip on stream" path cannot work, because `rl.event` only fires while RL runs, which is exactly when clips and OBS are stopped.

**R2c – MEDIUM – A crash restart ignores profile changes made during the backoff.**
`_on_crash` puts the plugin in `FAILED` (713). `apply_profile` skips `FAILED` records (555), and after the sleep `_spawn` runs with no profile re-check (723-725).
Scenario: twitch crashes in `stream`, and the user switches to `companion` within 2-8 s. Twitch is respawned under `companion`, outside its declared profile.

**R3 – HIGH (privacy) – Telegram sends to whoever messaged the bot last.**
`telegram.send` with an empty `chat_id` falls back to `_last_chat_id`, which is set by **any** inbound message, paired or not (`telegram/plugin.py:99-103,123`). `RemoteNotifier` always calls `send("", text)` (`remote/install.py:147-150`).
Scenario: a stranger finds the public bot username and sends "hi". The policy ignores them (`remote/service.py:137-142`), but from then on every notification ("Stream gestartet", "Fokus: …", "Komponente X: …", kill switch) goes to the stranger's chat.
Second effect: after any plugin restart, `_last_chat_id` is empty, so notifications fail until the phone writes first.
Third effect: `telegram.send` accepts an arbitrary `chat_id` at low risk / allow, so any caller of `plugin.tool.call` can use it as an exfiltration channel.

**R4 – MEDIUM – Twitch reconnect backoff never resets; no RECONNECT or auth handling.**
`_connect_once` only ever exits by raising (`irc_client.py:164-168`), so `backoff.succeeded()` (129) is unreachable. After roughly five drops over the process lifetime, every reconnect waits 30 s. `connected=True` and `twitch.connected` are emitted before the server has authenticated (160-163). A server `RECONNECT` (Twitch maintenance) is not handled and is only noticed at EOF.
`ReconnectBackoff` also has no jitter (`reconnect.py:66-70`). OBS and Home do reset, but a server that accepts and then closes cleanly yields a 1 s reconnect loop.

**R5 – HIGH (real environment) – Home Assistant frames over 1 MiB break the connection.**
`websockets.connect(url)` is used with the default `max_size=1048576` (`home/ws_client.py:103,176`; verified against websockets 17.1). `get_states` and `config/entity_registry/list` on a medium or large HA install (1000+ entities with attributes) exceed 1 MiB, which closes the socket with code 1009.
Scenario: every `home.list` or `home.light` call triggers `_refresh_inventory` (`home/plugin.py:205-214`), the socket drops, the future fails with `ConnectionError`, the plugin reconnects, and the next call repeats the cycle. The integration is unusable on real homes and has never been tested against a real HA.
Related: `_act` performs up to 4 sequential commands at 10 s each plus the service call (`plugin.py:263-289`), which can exceed the 30 s executor timeout. The user then sees a timeout while HA still switches the light afterwards.

**R6 – HIGH – OBS stream state is never resynced after connect or reconnect.**
`_on_obs_connected` only emits `obs.connected` (`obs/plugin.py:141-142`). `_streaming` changes only on `StreamStateChanged` edges (170-195).
Scenarios:
- Starting Nox after going live means no `stream.started`, so no DB session, no Funken booking (`booking.py:81-83`) and an empty clip `session_id`.
- If OBS crashes mid-stream, `_streaming` stays True, `stream.ended` is never emitted, and the session stays open. When streaming starts again, no new session is created.

**R7 – MEDIUM – The Home live view is empty until the first tool call.**
`_on_ha_event` forwards only entities already in `_inventory` (`home/plugin.py:188-190`). The inventory is filled only by tool calls, because `_on_connected` just resets the TTL (159-161). Entities added in HA are also not forwarded until the next refresh. Separately, `home.state` returns cached rows up to 5 s old (58, 205-214); `state_changed` events never update the cache.

**R8 – MEDIUM – Home never reconnects after a panic.**
`_on_panic` calls `client.stop()` (`home/plugin.py:128-136`), and nothing ever calls `client.start()` again. In practice the kill switch has also stopped the plugin (see R1/R1c).

**R9 – MEDIUM (security) – IRC CR/LF injection.**
`ChatSendInput.text` (`twitch/plugin.py:45-46`) and `send_privmsg` (`irc_client.py:198-201`) do no CR/LF stripping. The responder path strips newlines (`responder.py:140-148`), but `booking.py` and any `plugin.tool.call` caller do not.
Effect: a payload such as `"hi\r\nJOIN #other"` or `"\r\nPRIVMSG #victim :…"` injects raw IRC commands, and the moderation gate only checks keywords.

**R10 – MEDIUM – Every `!command` also triggers an LLM reply.**
`RelevanceClassifier` sets `addressed = mentions_bot or is_command` (`relevance.py:80`), and the responder answers anything addressed (`responder.py:73-77`). Result: `!rps rock` gets the RPS reply plus an LLM reply; so does another bot's `!discord`.
Bot-name matching is a substring test (`relevance.py:55`), so "equinox" or "noxious" count as mentions.
Head-of-line blocking: the LLM call runs inside the Twitch client's sequential hub event pump (`server.py:679-690`, `bus.py:4-8`), delaying `funken_awarded` and `!clip` events by the model latency.

**R11 – MEDIUM – Viewers can drain the bot's send budget.**
`!help`, invalid `!rps` choices, "wait Xs" cooldown replies (`twitch/plugin.py:183-223`) and `!funken` (`booking.py:101-117`) have no per-viewer cooldown and share the 20-per-30 s limiter. A few viewers can starve Nox's own replies.
In `_cmd_rps`, `minigame_ended{outcome: win}` is emitted before `_send`. If the send is rate-limited, it raises and `funken_awarded` is never emitted (225-249), so the win is recorded but no Funken are paid.

**R12 – MEDIUM (safety) – Scenes and switches bypass the Home boundary.**
`home.scene` is allowed without confirm in companion (`companion.yaml:62-65`), but the boundary checks only the `scene.*` id (`boundary.py:60-69`). An HA scene can set `lock.front_door: unlocked`. `home.switch` (allow) can drive a relay that operates a garage opener, and the boundary only knows cover device classes (`boundary.py:52-53`).
This contradicts "a language model must not be able to unlock a door" (`boundary.py:3-5`). The risk is contained today only because there is no model tool calling.

**R13 – MEDIUM – The Telegram poll loop can die silently.**
`_poll_loop` catches only `TelegramApiError` from `get_updates` (`bot.py:146-162`). An exception from `_on_connected` or `_dispatch` (for example `events.emit` after the hub is lost) ends the task with `connected=True`. The offset has already advanced before `_on_message` (168), so that message is lost. Telegram 429 `retry_after` is not honoured.

**R14 – MEDIUM – Home egress is fixed in the manifest while the host is "editable".**
`home.host` is presented as a Settings value (`defaults.yaml:302-308`), but only `127.0.0.1:8123` and `homeassistant.local:8123` pass egress (`home/manifest.yaml:57`, `companion.yaml:18,23`). The common setup (HA OS on `192.168.x.y:8123`, or wss via 443/Nabu Casa) requires hand-editing both the plugin manifest and the profile. Health does show the egress denial as its reason.

**R15 – HIGH (design, currently latent) – Coding sessions block the tool call.**
`session_start` awaits `run_with_repair` (`coding/plugin.py:171-184`), which can run 30 turns × (1 + 3 repairs) with no wall clock limit. `session_timeout_s` is never read. `_run`'s `readline` loop has no timeout (`session.py:405-420`). The core times out at 30 s (`manager.py:359`, `executor.py:28`), and the `session_id` is returned only at the end, so the caller can never poll or stop the session. There is no concurrency limit.
If the worker is terminated without a clean `stop()` on POSIX (no job object), the `claude` child is orphaned.

**R16 – LOW/MEDIUM – Coding stderr pipe is not drained.**
`proc.stderr.read(16 KiB)` (`session.py:401`) returns after the first chunk, and nothing reads further. With `--verbose` plus `--include-partial-messages`, once more than ~64 KB goes to stderr the child blocks on write and the session hangs until killed.

**R17 – HIGH (privacy) – Worker privacy state is not synced at spawn.**
`PrivacyView` defaults to BALANCED (`api.py:71`, `worker/plugin.py:76`) and is updated only by `privacy.mode_changed` events (`worker/plugin.py:106-112`). `plugin.register` returns config, profile and tools, but not the privacy mode (`manager.py:819-824`). The RL `CaptureGate` also starts open (`rl/plugin.py:90`).
Scenario: `privacy.mode: private` in `user.yaml`, or a plugin spawned after a crash or profile switch while PRIVATE/OFFLINE. Telegram starts long-polling `api.telegram.org`, Home connects to `homeassistant.local`, and RL captures the screen even with `capture.screen: false`. This violates the PRIVATE/OFFLINE promise, which is enforced only inside the worker.

**R18 – MEDIUM – Coding ignores privacy mode.**
The coding plugin has no privacy check. The `claude` CLI sends workspace code to Anthropic in any privacy mode, including OFFLINE. It is latent only because the tools are unreachable.

**R19 – LOW/MEDIUM – Prompt injection via chat.**
The responder wraps chat with `untrusted()` and offers no tools (`responder.py:123-137`), so injection can only shape the chat reply. The keyword moderation (`moderation.py`) is trivially evaded (spacing, leetspeak, other languages). The phone chat path (`remote/install.py:112-116`) reaches the full orchestrator with memory. That is intended for paired devices, but R3 widens who might see the output.

**R20 – LOW – Falsy-default bugs** (no `x or default` misuse on `__len__` objects was found in scope; the manager already fixes this at 431-439):
- `home/settings.py:48`: `api.config[key] not in (None, "", 0)` treats `tls: false` as "not pinned" (`False == 0`).
- `remote/policy.py:115`: `message.update_id and …` skips the replay check when `update_id` is 0.
- `telegram/bot.py:134`: `offset + 1 if offset else 0` (harmless).

**R21 – LOW – Twitch token-refresh health never reports failure.**
`_twitch_token_loop` calls `ensure_fresh_token()` and then unconditionally `health.succeeded()` (`settings/install.py:264-270`), but that function returns False instead of raising on failure (`twitch_auth.py:259-293`).

**R22 – LOW – RL capture cost.**
There is a `mss` instance and a full-frame BGRA → BGR copy per grab at up to 3 Hz combined. No handle leak (context manager, `calibration.py:134-141`; `creative/screenshot.py:167-169`), but 8-33 MB of allocations per grab on 1080p-4K screens. OpenCV vision runs in `to_thread` with no max-in-flight guard beyond the sequential loop.

---

## 4. Honest-status mismatches, TODOs and stubs

Mismatches (claim vs code):
1. `docs/PLUGIN_AUTHORING.md:5,96` say "no way to widen scope from inside your own code". A plugin can open raw sockets, read any file, subscribe to any non-redacted event, call any tool through `plugin.tool.call`, spawn processes and import `nox.*`. Isolation is only a process boundary plus an environment filter (manager comment at 180-182 admits "not an OS sandbox").
2. `clips/manifest.yaml:3-9` and `obs/plugin.py:17-21` say "no cross-plugin tool call". `manager.py:919-946` implements one, unaudited.
3. The Home plugin's "hard boundary: a model must not unlock a door" does not account for scenes, scripts or switches (R12).
4. OBS "security.panic path bypasses confirm" never runs (R1). Remote "critical notifications pass quiet hours" never sends (R1b).
5. Per-plugin honest `health()` methods are never called (§1.4).
6. `creative.screenshot.analyze` performs no analysis and captures the full monitor instead of the app window (`creative/manifest.yaml`, `plugin.py:136-169`).
7. `funken.py:8-9`'s "`twitch.funken.*` tool wraps it" does not exist. The earn-per-message/sub/bit/raid config is dead.
8. RL "coach": no audible callouts (no clip assets), and HUD templates are synthetic (disclosed in `recognizers.py`, but the product label "Coach" over-promises). `rl.match_ended.duration_s` is always 0.
9. `coding/manifest.yaml:41-45`: `session_timeout_s` is declared but unused. `filesystem_roots` is said to be "replaced by the user layer", but no mechanism merges user config into a plugin manifest (`manager.py:821` passes `manifest.config` only).
10. `rl/manifest.yaml:52`: `calibration_state_path` "(set by create)" is not set. The default is `~/.nox/rl/calibration.json` (`plugin.py:49-52`).
11. `twitch/manifest.yaml:37`: `api.twitch.tv:443` is "reserved, unused today" but still requested egress.
12. `obs/plugin.py:176-181`: `stream.started.twitch_connected` is hard-coded False. `scene_changed.by` is always `"manual"` (154), even when Nox switched the scene.
13. `manager.py:5-8` says "crashes are restarted…". It does not detect a lost hub connection (R2).
14. `docs/PLUGIN_AUTHORING.md:121` says medium risk "already asks to confirm". Profiles pre-approve medium Home verbs (`companion.yaml:48-72`). This is fine, but should be documented.

TODO / stub / placeholder inventory (in scope; there are no literal `TODO`/`FIXME` markers and no `raise NotImplementedError`):
- `plugins/rl/src/nox_plugin_rl/vision/onnx_stub.py:1-43`: inert ONNX backend, always `UNAVAILABLE`, zero detections.
- `plugins/rl/src/nox_plugin_rl/recognizers.py:5-13`: synthetic templates pending ES-04 real screenshots.
- `plugins/rl/src/nox_plugin_rl/plugin.py:192-195`: overtime/demo banner classification and live GPU/FPS budget reduction "not implemented in this pass". **No real-time `demo` or `overtime` events are ever emitted, so the `rl.callout.demo` and overtime rules (`callouts.py:57-85`) are unreachable.**
- `plugins/rl/src/nox_plugin_rl/callouts.py:6-10,51-54`: the position-based callout list is not reachable, and kickoff is not wired.
- `plugins/rl/manifest.yaml:74` (`min_confidence` not PO-confirmed); `:71-72` (onnx stub).
- `src/nox/rl/capture_gate.py:28-31`: stale "open point".
- `src/nox/rl/services.py:437`: prompt text "deeper pattern analysis is not yet available".
- `plugins/creative/src/nox_plugin_creative/artifact.py:270-273`: Blender stats "not implemented in this pass".
- `plugins/creative/manifest.yaml:47-52`: DaVinci and browser-DAW patterns speculative.
- `plugins/obs/manifest.yaml:47-57`: `privacy_scene`/`scene_set` "pending approval ES-02".
- `plugins/twitch/manifest.yaml:51-54`: RPS rates "pending approval". `src/nox/stream/funken.py:7`: placeholder rates.
- `plugins/clips/src/nox_plugin_clips/plugin.py:21-24`: hype categories pending; the event is never produced.
- `src/nox/plugins/manager.py:938-944`: plugin-initiated confirmations "not implemented".
- `src/nox/clips/trim.py:1-4,16-19`: ffmpeg not bundled.
- `docs/PLUGIN_AUTHORING.md:103-105`: dependency packaging "not built yet".

---

## 5. Extension points

**What a new plugin needs today:**
1. `plugins/<id>/manifest.yaml` (schema §1.1) with tools declared at their exact risk.
2. `src/<pkg>/__init__.py` exposing `create(api)`, optionally with `start`/`stop`.
3. Egress entries added to **both** the manifest and the target profile's `egress_allowlist`/`loopback_allowlist`.
4. Its secrets set via `nox secrets set nox/<id>/…`.
5. Its id added to `plugins.enabled`.
6. Its Python dependencies installed into the **core venv** (no per-plugin packaging).
7. For raw sockets, a call to `api.egress.authorize(host, port)` before every connect (voluntary).
8. For any user-facing effect, core-side glue as well: an IPC request plus a dashboard page (as `home` and `clips` do), or a bus subscriber (as `remote`, `rl` and `stream` do). The model cannot call plugin tools.

**Architectural blockers shared by every new domain:**
- **B1:** No LLM tool-use loop in the orchestrator. Every agentic domain needs bespoke IPC plus UI, or a deterministic intent layer (only `home` has one).
- **B2:** No plugin-initiated confirmation (`manager.py:938-944`). A plugin cannot ask the user before a side effect it starts itself (for example "send this mail?").
- **B3:** Workers cannot write secrets, so OAuth refresh must live core-side (the Twitch pattern, `settings/twitch_auth.py`). There is no generic OAuth broker, so each new provider needs its own core service.
- **B4:** 30 s synchronous tool timeout with no job/async-handle pattern and no streaming of tool results (R15).
- **B5:** No real sandbox; raw-socket egress is voluntary. Third-party plugins cannot be trusted with anything sensitive. Reserved event namespaces are missing (§1.2).
- **B6:** Privacy mode is not synced at spawn (R17). Every egress-capable plugin inherits this flaw.
- **B7:** Mode→profile coupling stops plugins mid-activity (R2b). Features cannot compose across profiles.
- **B8:** Plugin tools carry `targets=None` (`manager.py:846`), so profile rules cannot match a target (path, recipient, entity). All scoping has to be done ad hoc inside handlers.

**Natural next domains:**

| Domain | Fit | Specific blockers |
|---|---|---|
| Calendar / mail via local connectors (Outlook COM, Thunderbird, CalDAV/IMAP on LAN) | Good: read-mostly, loopback or LAN | B1 (no way to ask "what's on today" by voice), B2 for sending mail, B3 if cloud (Google/M365 OAuth), B8 (no recipient-level rules). Mail bodies are prompt-injection carriers, so the `untrusted()` wrapping needs a generic tool-result path, which doesn't exist without B1. |
| Browser automation (Playwright; the dependency was bumped in #33) | Risky | B5 is decisive: Chromium's network cannot be scoped by `PluginEgressGuard`. B2 is required for form submits. B4 because pages take longer than 30 s. The hard-prohibition list has no browser verbs. |
| Discord | Good, mirrors Twitch | Needs a gateway WS with resume and sequence handling, which is exactly the class of R2/R4 bugs. `twitch.chat_message`-style redaction rules (`server.py:84-90`) must be extended. RemoteService hard-codes the `telegram` channel/origin (`service.py:38,96`). B3 for bot token rotation is simple, OAuth is not. |
| Spotify / media | Moderate | Web API needs OAuth PKCE and Premium (B3). The local alternative is HA `media_player` (already present) or Windows SMTC via winrt (a new dependency, see the packaging gap). B1 for "play X". |
| File management | Moderate | Profiles already have `filesystem_roots`, but plugin tools do not get them (B8). A plugin can read any file (B5). A quarantine-delete pattern exists core-side (`companion.filesystem.delete` rule), but there is no plugin API for it. |
| PC control (input, window management) | High risk | Conflicts with the RL "observation only" guarantee: input synthesis must be profile-denied in `rocket_league`, but the hard-prohibition list only names `game.input.send`. B2, B5. The creative plugin deliberately omits `mouse.*`/`keyboard.*` (`creative/manifest.yaml:4`), so there is precedent for a separate high-risk plugin. |

---

## 6. Test coverage map

| Area | Unit | Integration | Real-env | Notable holes |
|---|---|---|---|---|
| Runtime (`manifest`, `api`, `lifecycle`) | 19 + 13 + 23 | `test_plugins.py` (5, real subprocess echo) | n/a | No tests for: profile change during crash backoff (R2c); hub disconnect → zombie (R2); privacy sync at spawn (R17); kill-switch resume → respawn (R1c); `plugin.tool.call` audit or cross-plugin calls; `register_timeout_s`. |
| echo | (runtime tests) | yes | n/a | none |
| twitch | 71 (`tests/unit/plugins/twitch`, 9 files) | 3 (fake IRC) | none | CR/LF, RECONNECT/USERNOTICE, raid bursts against the hub rate limit, backoff reset |
| obs | 33 | 3 (fake OBS) | 1 skipped (`@network`) | Panic via the real kill-switch ordering; resync on connect |
| home | 41 plugin + 57 core | 7 (fake HA) | 1 skipped | Frames over 1 MiB, large inventories, scene/switch boundary, reconnect after panic |
| telegram / remote | 20 + 62 | 13 | none | `_last_chat_id` from an unpaired sender; kill-switch reply path; poll loop crash |
| coding | 16 | 3 (`fake_claude.py`) | none | Long session vs the 30 s timeout; stderr flooding; privacy mode |
| rl | 27 plugin + 47 core | 3 + 5 (vision) | none (no real HUD screenshots, ES-04) | Mode bridge from `stream`; profile restored on `game.ended`; clip assets present |
| clips | 16 plugin + 35 core | 1 | none | Confirm requirement on `replay_buffer.save` under the real stream profile; `!clip` flood |
| creative | 29 plugin + 18 core | 5 | none | Multi-monitor, zone vs full-screen capture |
| stream core | 27 | 2 (`test_stream_core.py`) | n/a | Command → LLM double reply; head-of-line blocking |

In total: about 480 unit tests and about 57 integration tests across the scope. **None exercise a real Twitch, OBS, Home Assistant, Telegram, Claude CLI or Rocket League HUD.** The only real-environment tests are the two skipped `@pytest.mark.network` tests.
