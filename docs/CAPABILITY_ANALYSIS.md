# Nox capability analysis

State of the tree at `3af0a5a` (after the cross-platform work on this branch). The analysis
underpins [`SPEC_V3.5_V4.5.md`](SPEC_V3.5_V4.5.md).

> **Kurzfassung (Deutsch).** Nox ist **breit, aber dünn**. Es gibt sehr viel Code, und das
> Fundament (IPC, Konfiguration, Permission-Engine, Egress pro Privacy-Modus, Tests) ist solide.
> Aber drei Dinge stimmen nicht mit dem überein, was das Projekt über sich sagt:
>
> 1. **Nox ist kein Agent.** Kein Sprachmodell kann heute ein Werkzeug aufrufen. 21 von 36
>    Plugin-Tools haben keinen einzigen Aufrufer: Coding-Agent, Rocket-League-Tools, OBS-Steuerung
>    und Creative-Analyse sind vorhanden, aber für niemanden erreichbar.
> 2. **Mehrere Garantien „unterhalb des Modells" gelten in der Praxis nicht.**
>    - Eine PIN kann nirgends gesetzt werden, also ist jedes PIN-Gate wirkungslos.
>    - Das `work`-Profil schickt Chats trotz „no cloud" in die Cloud.
>    - Jeder Hostname, der mit `127.` beginnt, gilt als lokal.
>    - Plugins starten im Glauben, der Privacy-Modus sei BALANCED.
>    - Privacy-Modus und Kill-Switch gehen bei einem Neustart verloren.
> 3. **Wiederanlauf ist ungeprüft oder kaputt.**
>    - Ein abgestürzter Voice-Worker kommt nie zurück.
>    - Worker-Tokens sind einmalig, ein Reconnect ist also unmöglich.
>    - Der Safe-Mode des Supervisors ist eine Falle ohne Ausgang.
>    - Die zugesagte Datenlöschung nach 7 Tagen läuft nie.
>
> Nichts davon ist gegen echte Dienste erprobt: Home Assistant, OBS, Twitch, Telegram und
> Audio-Hardware liefen bisher nur gegen Fakes. Die richtige Konsequenz ist nicht „mehr Features",
> sondern: **erst wahr machen, was behauptet wird (v3.5), dann handlungsfähig werden (v4.0),
> dann im echten Einsatz beweisen (v4.5).**

## 1. Method

| Step | What |
| --- | --- |
| Subsystem audits | Six read-only audits, one per area, each citing `file:line` for every claim: [core](analysis/audit_A_core.md), [security](analysis/audit_B_security.md), [AI & memory](analysis/audit_C_ai.md), [voice, sensors & shell](analysis/audit_D_voice_shell.md), [plugins & domains](analysis/audit_E_plugins.md), [UI, CI & delivery](analysis/audit_F_ui_ci.md). |
| Hand verification | Every finding in the register below marked **✔** was re-checked against the code by hand; several were reproduced with a failing test (**✔✔**). |
| Executed | On Linux: ruff, mypy (233 files, 0 errors), 1809 unit tests incl. the Qt shell off-screen, 82 integration tests, 432 UI tests (pet 215, dashboard 217), eslint, tsc, vite builds, `npm audit` (0 vulnerabilities), secrets scan, link check, forbidden-API guard. |
| Not executed | Anything on Windows or macOS, any real external service (Home Assistant, OBS, Twitch, Telegram, Ollama, Claude Code CLI), any audio hardware, the installer. |

Maturity scale used throughout: **WORKS** (works and is tested) · **UNPROVEN** (works against fakes,
never against the real thing) · **LIMITED** (works partially or with a caveat the user must know) ·
**SCAFFOLDING** (structure exists, the capability does not) · **UNREACHABLE** (works in isolation,
but no user path leads to it) · **MISSING**.

## 2. Verdict

Nox is an unusually disciplined codebase: typed contracts, a composition root with named boot
steps, honest health reporting as a design principle, a permission engine with ordered guards, an
egress guard, a hash-chained audit log, and ~1,900 Python plus 432 UI tests. The *mechanisms* are
mostly sound.

The gap is between mechanism and product. Three patterns repeat across every area:

1. **Wiring gaps.** A correct mechanism exists, but nothing connects it to the user or to the
   place that needs it: the PIN can be verified but never set; the profile says `cloud_allowed:
   false` but the router is only told about the privacy mode; `ToolRegistry.describe()` produces a
   perfect model tool list that nobody sends to a model; retention `purge_*` methods that nothing
   schedules.
2. **Edge-triggered state.** Consumers learn state from *change events* only, so anything that
   starts late or reconnects believes the permissive default: plugins start as BALANCED, the
   orchestrator clears safe mode on `system.started`, the voice worker opened the microphone
   against the current zone (fixed on this branch), and nothing survives a restart.
3. **Unproven edges.** Every integration with the outside world has been exercised only against
   fakes, and every recovery path (reconnect, respawn, resume, corrupted database) is either
   untested or broken.

This is why "make it able to do everything, reliably" cannot start with new features: most of what
Nox *already claims* is not yet reliably true.

## 3. Capability matrix

| Area | What a user can really do today | Maturity | Main gap |
| --- | --- | --- | --- |
| **Core platform** (supervisor, IPC hub, config, state, health, DB) | Start Nox; UIs connect over an authenticated, typed WebSocket; health reports per component | WORKS (in-process) / broken recovery | Worker tokens single-use → no reconnect; supervisor safe mode has no exit; damaged DB header crashes boot before rename-aside; no single-instance guard; no backup before migrations |
| **Cross-platform** | Windows 11; macOS and Linux from source ([PLATFORMS.md](PLATFORMS.md)) | WORKS (Windows, Linux CI) / UNPROVEN (macOS, real Linux desktops) | macOS CI not yet blocking; Wayland is fail-closed by design |
| **Security: permission engine, egress per privacy mode, secret redaction** | Tool calls are allowed/confirmed/denied in code; outbound connections are checked | WORKS | Callers bypass it (see register) |
| **Security: PIN, profiles, audit, persistence** | — | **Not effective** | PIN never settable; profile restrictions not enforced for chat; audit truncation undetected; privacy/kill state lost on restart |
| **Kill switch** | Tray, dashboard, hotkey (Windows/X11/macOS with permission), spoken phrase | LIMITED | No `sup.resume` sender; Resume button missing; spoken phrase both misses and false-triggers |
| **Chat** (dashboard text, voice) | Ask questions; answers stream sentence by sentence; fast path for greetings/time/date; Claude Code → Ollama → rules fallback | WORKS (single-shot) / UNPROVEN with real providers | No tools; history only last 8 turns of the current run; Claude health probe sends a paid request every 30 s, also in offline/private mode |
| **Agentic actions** (model decides and acts) | — | **MISSING** | Claude Code runs with `--tools ""`, Ollama is never sent `tools`; the prompt claims tool calls exist → invites fake "Erledigt" |
| **Memory** | Cosine retrieval over the user's own vault notes | LIMITED | "Merk dir das" is detected but never stored; no cross-session memory; missed embeddings never retried; vault text enters the system prompt unwrapped (injection) |
| **Proactive / reminders / scheduling** | — | UNREACHABLE / MISSING | `ProactiveService.notify` has zero callers; no timers or reminders |
| **Project manager** | Indexes work items from the vault | UNREACHABLE | No UI, no conversation path |
| **Voice** | Push-to-talk (default), local Whisper STT, Piper/Kokoro TTS, wake word via transcript text | LIMITED / UNPROVEN on hardware | No "Nox" wake-word model; barge-in stops only the current sentence; no echo cancellation; crashed worker never respawned; Whisper downloads its model although docs say nothing is downloaded |
| **Desktop pet** | Animated WebGL rig, moods, click-through, sprite variants | WORKS | Pet does not recover from WebGL context loss; key poses have no art |
| **Dashboard** | Chat, Zuhause, Stream, Clips, Remote, Settings, toasts, audit (current connection only) | WORKS (UI tests) | No conversation history, memory browser, plugin manager, notification history, PIN setup, privacy-FULL confirm, resume; token rotation leaves an open dashboard dead |
| **Home Assistant** | Dashboard: list, toggle, scenes, media, climate, covers; German/English sentences matched locally in <1 ms | UNPROVEN (fake HA only) | Intent matcher not reachable by voice/chat; 1 MiB WS frame limit likely breaks large installs; scenes/switches can bypass the lock/garage boundary |
| **Twitch** | Bot in chat, `!rps`, `!help`, `!funken`, LLM replies | UNPROVEN | No CR/LF sanitising (IRC injection); every `!command` also triggers an LLM reply; zombie after hub loss |
| **Funken economy** | Earn 5 per `!rps` win, check balance, leaderboard | LIMITED | Other earn rates are dead config; nothing to spend on; daily cap resets on restart |
| **OBS** | Stream start/stop detection drives sessions | UNPROVEN | 6 of 7 tools unreachable; panic → privacy scene never runs in the real order |
| **Telegram / phone** | Pair, `/status`, `/kill`, `/privacy`, free-text chat | UNPROVEN | Notifications can reach an unpaired stranger |
| **Coding agent** | — | UNREACHABLE | No UI or conversation path; `session.start` blocks past the 30 s tool timeout |
| **Rocket League coach** | Game detection, replay header parsing, mode switch | LIMITED / SCAFFOLDING | HUD recognition uses synthetic templates; no callout audio ships; no real coaching output |
| **Clips** | Library: list, tag, export, trim (with ffmpeg) | WORKS | `!clip` spammable by any viewer; hype trigger never fed |
| **Creative** | App detection switches mode | SCAFFOLDING | "screenshot.analyze" performs no analysis and captures the whole monitor |
| **Onboarding** | `nox onboard` CLI wizard | WORKS | CLI only; does not set a PIN |
| **Delivery** | Run from source | — | Installer layout likely cannot start; no update/rollback code; nothing signed; 3 of 20 release-checklist items done; versions disagree (0.1.0.dev0 / 0.2.0 / UI 0.1.0) |
| **CI** | Lint, types, unit, integration, UI, e2e smoke (2 tests), license, secrets, forbidden-API guard, now Linux + macOS | WORKS | No vulnerability scan, coverage floor, UI lint in CI; `python-posix` not yet a required check |

## 4. Findings register

Severity per [`CODE_STANDARDS.md`](CODE_STANDARDS.md) §9. **✔** = re-checked by hand, **✔✔** =
reproduced with a test that fails before the fix. "Target" is the version in the specification that
owns it.

### 4.1 Guarantees that do not hold

| ID | Sev | Finding | Evidence | Check | Target |
| --- | --- | --- | --- | --- | --- |
| G1 | Critical | No product path sets a PIN; every PIN gate (relaxing privacy, secrets, security-path resume) is dormant. `nox secrets set nox/security/pin` stores a raw value that then locks every gated change. | `security/secrets.py` `set_pin` has no non-test caller | ✔ | 3.5 |
| G2 | Critical | Profile `cloud_allowed`/`memory_writes_allowed`/`integrations_allowed` are not enforced for chat: `work` and `rocket_league` send conversations to the cloud in BALANCED mode. | `app.py` passes only `privacy.allows_cloud` to the router | ✔ | 3.5 |
| G3 | High | Egress: any host starting with `127.` counts as loopback (`127.evil.example`). | `core/netloc.py` `is_loopback` | ✔ | 3.5 |
| G4 | High | Plugins start believing privacy is BALANCED and learn the real mode only from later changes. | `worker/plugin.py` | audit B F3 | 3.5 |
| G5 | High | Orchestrator clears safe mode on `system.started`: after a boot-time audit break Nox still chats. | `core/orchestrator.py` `_on_started` | ✔ | 3.5 |
| G6 | High | Privacy mode, panic and kill state do not survive a restart; Nox becomes less private after a crash. | audit B F9 | audit | 3.5 |
| G7 | High | Audit chain does not detect tail truncation or checkpoint rollback; failed audit writes are swallowed. | `security/audit.py` | audit B F7 | 3.5 |
| G8 | High | Plugin isolation is cooperative: egress, subscriptions and emitted namespaces are enforced inside the plugin; `plugin.tool.call` runs core tools unaudited. | audit B F4, audit E §1 | audit | 3.5 |
| G9 | Medium | Supervisor trusts a self-reported pid as "the core"; shell, pet and dashboard share one token and choose their role. | audit B F6 | audit | 3.5 |
| G10 | Medium | PIN hash is PBKDF2; docs promise Argon2id (`argon2-cffi` is not a dependency). | `pyproject.toml` | ✔ | 3.5 |
| G11 | Medium | Home boundary is domain-level: auto-allowed scenes can set `lock.*`; garage relays as `switch.*` are controllable. | audit B/E | audit | 3.5 |
| G12 | Medium | `sensors.enabled: false` silently disables privacy zones on every platform. | `sensors/install.py` | ✔ | 3.5 |

### 4.2 Recovery and reliability

| ID | Sev | Finding | Evidence | Check | Target |
| --- | --- | --- | --- | --- | --- |
| R1 | High | Worker tokens are consumed on first use, so a worker can never re-authenticate after a hub drop; a crashed voice worker is never respawned; health says "worker starting" forever. | `ipc/tokens.py` (`pop` on first use) | ✔ | 3.5 |
| R2 | High | No client sends `sup.resume`: after a hotkey kill the supervisor stays in safe mode and never again restarts a crashed core. | no sender outside `supervisor/` | ✔ | 3.5 |
| R3 | High | Retention never runs: transcripts, chat, viewer data and health history are kept forever, contradicting PRIVACY.md's 7 days. | `purge_all_expired` has no caller | ✔ | 3.5 |
| R4 | High | Damaged DB header fails boot before the rename-aside logic; no backup before migrations; one bad key in `user.yaml` discards the whole user layer. | audit A §3 | audit (verified experiment) | 3.5 |
| R5 | High | Plugin worker losing the hub stays alive as a zombie reporting AVAILABLE. | `worker/plugin.py` | audit | 3.5 |
| R6 | Medium | Health loop every 30 s re-hashes the whole audit chain on the event loop, runs `PRAGMA integrity_check` under the DB lock, and sends a paid Claude request even offline. | `core/boot/health.py` | audit | 3.5 |
| R7 | Medium | Router health-probes every provider sequentially before streaming: first turn after cache expiry waits for a Claude round trip. | `ai/router.py` | audit | 3.5 |
| R8 | Medium | Voice: barge-in stops only the current sentence; follow-ups in the conversation window are dropped; kill phrase misses variants and fires on quotes. | audit D R1–R8 (confirmed with fakes) | audit ✔ (fakes) | 3.5 |
| R9 | Medium | UI: hard-coded `clientVersion: '0.1.0'` vs a major-version check → a 1.x core rejects its own UIs; token rotation leaves an open dashboard permanently `auth_failed`. | `ui/*/src/ipc.ts`, `ipc/server.py` | ✔ | 3.5 |
| R10 | Medium | Starting a second supervisor deletes the running one's token file; no single-instance guard. | audit A | audit | 3.5 |

### 4.3 Claims the documentation makes that the code does not keep

| ID | Claim | Reality | Target |
| --- | --- | --- | --- |
| D1 | USER_GUIDE: "ships as a signed Windows installer … update channels" | No updater, no signing; installer layout likely cannot start | 3.5 (text), 4.5 (product) |
| D2 | PRIVACY: transcripts expire after 7 days | Retention never scheduled (R3) | 3.5 |
| D3 | SECURITY: PIN is Argon2id | PBKDF2 (G10) | 3.5 |
| D4 | README/voice: "nothing is ever downloaded on its own" | faster-whisper fetches its model on first start, also offline | 3.5 |
| D5 | CHANGELOG: barge-in fixed | only the current sentence stops (R8) | 3.5 |
| D6 | README: `work` profile "no cloud" | not enforced for chat (G2) | 3.5 |
| D7 | System prompt: "actions happen only through typed tool calls" | no tool calling exists → model claims actions it never did | 3.5 (text), 4.0 (product) |

### 4.4 Fixed on this branch

| Finding | Commit |
| --- | --- |
| Nox ran on Windows only; non-Windows hosts built no foreground sensor and reported privacy zones as working | `ca878de` |
| Audit writer could COMMIT inside another thread's open SQLite transaction (✔✔) | `ca878de` |
| Quit from the shell made the watchdog spawn a fresh core that shutdown killed mid-boot (✔✔) | `ca878de` |
| Supervisor left a safe-mode core as a zombie on POSIX; shutdown could close the DB under a draining audit writer | `ca878de` |
| Integration tests read the developer's real credential store and real foreground window | `ca878de` |
| Voice worker opened the microphone against a zone/disabled mic/kill that predated its connection (✔✔) | `3af0a5a` |

## 5. What is solid

Worth protecting while everything else changes:

- **IPC hub**: typed, versioned envelopes, per-client tokens, frame budgets, authentication
  timeout, per-client event pumps.
- **Permission engine**: ordered guards, hard prohibitions by name, `..` and prefix-grant fixes.
- **Core egress guard** per privacy mode (apart from G3), shared SSL context.
- **Secret handling**: keyring only, redaction at every nesting level in logs and audit, no
  `secrets.get` over IPC.
- **Honest health** as a design principle; `nox doctor`.
- **Configuration**: layered, strictly validated, round-tripping user layer.
- **Deterministic home intent matcher** (<1 ms, no model), **replay header parser**, **clip
  library**, **remote pairing** design (single-use code, salted hash, atomic redeem).
- **Test culture**: fakes over hardware, deterministic clocks, UI state reducers tested.

## 6. Why "everything, reliably" is the wrong target - and what to aim for instead

"Everything" has no finish line and no test, so it cannot be reliable by construction. Nox's
own design already rejects part of it on purpose: no input into games, no unlocking doors, no stream
stop, no scene delete. Those are not gaps and the specification keeps them.

The useful version of the goal is: **every capability Nox advertises is reachable, permitted below
the model, observable, recoverable, and proven against the real thing** - and the set of
capabilities grows through one controlled mechanism (a model-driven tool loop under the permission
engine) instead of one-off features. That is what [`SPEC_V3.5_V4.5.md`](SPEC_V3.5_V4.5.md)
specifies.
