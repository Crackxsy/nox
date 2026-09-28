> **Provenance.** Machine-assisted subsystem audit of the tree between `6625b50` and `3af0a5a` (read-only, code-reading
> with a few scratch experiments), written for [`../CAPABILITY_ANALYSIS.md`](../CAPABILITY_ANALYSIS.md).
> Line numbers may be off by a few lines for files changed since. Findings that the consolidated analysis relies on were re-checked
> by hand and are marked there; treat everything else here as a well-sourced lead, not a verdict.

# Audit A — Core platform (capability + reliability)

Scope: `src/nox/core/**`, `supervisor/`, `worker/`, `ipc/**`, `data/**` (+ migrations), `util/`, `health/`, `app.py`, `cli.py`, `entrypoints.py`, `paths.py`, `config/defaults.yaml`, matching tests. Read-only, code-reading audit at HEAD `ca878de` (the cross-platform working-tree changes were committed as "feat(platform)…" while this audit ran; line numbers refer to that state; a concurrent in-flight edit to `ipc/handlers/core.py` and `worker/main.py` (capture/safe-mode state on `worker.register`) was reflected in the line numbers and does not change any finding). The full test suite was not run. Two small SQLite experiments were run in the scratchpad (noted inline as **[verified]**).

Already known and not re-reported: audit writer shares the DB lock; supervisor reaps zombies; POSIX parent watch; stray `.hc.py`; `sensors.enabled=false` disables privacy zones.

---

## 1. Capability inventory

Maturity key: **W+T** = works + tested · **W-U** = works, unproven in a real environment (only fakes or unit level) · **LIM** = limited · **SCAF** = scaffolding · **MISS** = missing.

| Capability | What a user/subsystem can actually do | Entry points | Key files | Maturity | Test evidence |
|---|---|---|---|---|---|
| Supervisor / watchdog | Spawns core + shell. A core that exits, or misses 5 or 10 heartbeats (after the 90 s boot grace), is restarted. More than 3 restarts in 300 s puts the supervisor in safe mode. | `nox supervisor`; control channel `sup.auth/auth_ok/heartbeat/kill/stop/ack/status/resume/error` | `supervisor/main.py`, `supervisor/messages.py`, `supervisor/client.py` | W-U (tests use `tests/unit/supervisor/fake_core.py`, never the real `NoxCore`) | `test_supervisor.py` (18), `test_client.py` (7), `test_core_kill_handler.py` (3) |
| Global kill-switch hotkey | `ctrl+alt+shift+k` goes to `sup.kill` and on to the core kill switch. If there is no ack within 2 s, the tree is killed and the shell relaunched. | pynput hotkey in the supervisor; shell tray via `sup.kill` | `supervisor/main.py:312-331,750-783` | W+T at unit level; LIM (safe mode has no way back, see R-2) | `test_kill_switch_*` |
| Leave supervisor safe mode | `Supervisor.resume()` restarts core + shell | `sup.resume` — **no client ever sends it** (grep: only `supervisor/`) | `supervisor/main.py:333-351` | SCAF (reachable only through a hand-written socket client) | Called directly in tests |
| Core composition / boot / stop | 16 ordered boot steps, reverse-order stop | `nox core`, `python -m nox.app`, `nox dev` | `app.py:184-203,525-614`, `core/boot/*` | W+T | `tests/integration/test_walking_skeleton.py` and about 20 integration files |
| Config (4 layers) | defaults → user.yaml → profile → runtime overrides; env/`${NOX_APP_DIR}` expansion; an invalid layer is dropped with a warning | every entry point | `core/config/loader.py`, `core/config/*.py`, `paths.py` | W+T | `test_config.py` (17), `test_app_dir.py` (10) |
| Event bus | Async pub/sub, glob patterns, priority lane (`security.*`, `system.*`), capacity 10 000, drops `stream.*`/`sensor.*` when full | `AsyncEventBus.publish/subscribe/wait_for` | `core/bus.py`, `core/events.py` | W+T | `test_bus.py` (11), `test_events.py` (3) |
| Live state + checkpoints | Versioned `NoxState`, `state.changed` events, debounced or immediate SQLite checkpoints, restore at boot | `state.get` IPC, `/api/state` | `core/state.py`, `core/statemgr.py` | W+T (restore semantics questionable, R-15) | `test_statemgr.py` (7) |
| Health service | Periodic probes (every 30 s) with timeouts, `system.health_changed`, `health.report`, history rows | `health.get`, `/health`, `health.history` | `core/health.py`, `core/boot/health.py`, `health/checks.py` | W+T in unit tests; the integration fixtures set `check_interval_s: 3600` (`test_walking_skeleton.py`), so the periodic loop is never exercised | `test_health.py` (3), `health/test_checks.py` (9) |
| Degraded modes | `system.level` running⇄degraded for 4 components (disk, vault, audit chain, config). Disk-full self-repair runs memory retention. Audit break escalates to the kill switch. | bus only | `health/degraded.py`, `health/install.py` | W+T (narrow: 4 components, 1 repair action) | `health/test_degraded.py` (5) |
| IPC hub | Loopback WS: auth within 3 s, role/handler ACL, rate limit 50/s (burst 200), 1 MiB frames, subscribe, per-role redaction, core→worker requests, bounded send/event queues | `ipc.auth`, `ipc.ping`/`ipc.pong`, `ipc.subscribe`, `ipc.error` | `ipc/server.py`, `ipc/dispatch.py`, `ipc/protocol.py` | W+T | `test_server.py` (33), `test_dispatch.py` (16), `test_server_event_pump.py` |
| IPC client | Auth, request/stream, subscriptions, auto-reconnect; Qt thread runner | used by shell, workers, plugins | `ipc/client.py` | W+T for session roles; **workers cannot reconnect** (R-1) | `ipc/test_client.py` (8, including an assertion that worker reconnect is denied, `:108-130`) |
| Tokens | Per-start session token file (0600 / icacls); one-time worker tokens (TTL 60 s) | env `NOX_WORKER_TOKEN` | `ipc/tokens.py` | W+T (roles among session-token holders are self-asserted, R-13) | `test_tokens.py` (13) |
| HTTP server | `/health` (no auth), `/api/state`, `/api/providers` (Bearer), `/pet`, `/dashboard` static, security headers | HTTP port 47801 (+5 fallbacks) | `ipc/http.py` | W+T | `test_http.py` (8), `test_health_ws_port.py` |
| Voice worker lifecycle | Spawn on boot or resume; register → ready; detach on disconnect; terminate | `worker.register`, `worker.ready`, `worker.heartbeat` | `core/boot/workers.py`, `worker/main.py`, `worker/heartbeat.py` | LIM: no respawn on crash, heartbeats ignored, reconnect impossible | Worker logic tested with `FakeIpcClient` only (`tests/unit/voice/test_worker_reregister.py`); `WorkerSupervisor` has **0** tests |
| Plugin worker runtime | Loads manifest, `create(api)`, `plugin.register`, answers `tool.call` / `plugin.stop` | `python -m nox.worker --plugin <id>` | `worker/plugin.py` | W-U (no reconnect, no exit on disconnect: R-5) | Plugin integration tests |
| Database + migrations | One connection + RLock, WAL, 9 migrations each in one transaction, integrity check at boot with rename-aside | — | `data/db.py`, `core/boot/persistence.py`, `data/migrations/*.sql` | W+T; LIM for header corruption (R-7) | `test_db.py` (11), `test_repos.py` (8), `test_shared_connection.py` (1) |
| Retention purge (turns, chat, viewers, health history, grants, audit) | Repository methods exist; **nothing ever calls them** | none | `data/repos.py:241`, `data/stream_repos.py:520-534` | **MISS** (R-6) | Repository-level tests only |
| DB backups / rollback | `paths.backups_dir` is configured and never used | none | `core/config/core.py:63-66,92` | **MISS** | — |
| Orphan protection | Windows named job object with kill-on-close; POSIX `NOX_PARENT_PID` watch (1 s poll, 10 s grace) | automatic | `core/jobobject.py`, `core/parent_watch.py` | W+T (unit) | `test_jobobject.py` (3), `test_parent_watch.py` (8) |
| Structured logging + PII filter | JSON daily rotation, secret/PII masking | core `nox.log`, `supervisor.log` | `core/logging.py` | LIM: worker, plugin and shell processes never call `configure_logging` (R-17) | `test_logging.py` (7) |
| Background task queue | Persisted, prioritised, pauses in game | used only by `rl/install.py:161` | `core/tasks.py` | W+T (single consumer) | `test_tasks.py` (3) |
| `nox doctor` | Config, folders, provider health (through the egress guard), voice imports | CLI | `entrypoints.py:368-455` | W-U (0 tests) | none |
| Orchestrator | Text or voice turn → router stream → sentence TTS → turn store; fast path; barge-in cancel | `chat.send`, `voice.transcript_ready` | `core/orchestrator.py` | W+T; safe-mode gate bypassable (R-3) | `test_orchestrator.py` (15), walking skeleton |

### 1a. All IPC request methods (hub-side registrations)

| Name | Roles | Registered at |
|---|---|---|
| `state.get` | shell, dashboard, pet, plugin (pet/plugin filtered to `assistant`/`privacy`/`system`) | `ipc/handlers/core.py:173` |
| `health.get`, `mode.set`, `voice.mute`, `chat.send`, `ai.providers`, `plugin.status`, `stream.session.status`, `stream.funken.top` | shell, dashboard | `handlers/core.py:174-192` |
| `privacy.set`, `security.kill`, `security.panic`, `security.resume` | shell, dashboard, supervisor (the `supervisor` role can never authenticate on the hub: `tokens.py:189-195`) | `handlers/core.py:176-179` |
| `security.permission.reply`, `voice.ptt` | shell | `handlers/core.py:180-181` |
| `pet.interact` | pet, shell | `:185` |
| `worker.register`, `worker.ready` | worker | `:187-188` |
| `worker.heartbeat` | worker, plugin (no-op handler) | `:189`, `:429-430` |
| `plugin.register`, `plugin.secret.get`, `plugin.tool.call` | plugin | `plugins/manager.py:470-478` |
| `health.history`, `config.effective` | shell, dashboard | `proactive/install.py:110-121` |
| `config.get`, `config.set`, `security.pin.status`, `secrets.status`, `secrets.set`, `secrets.delete`, `twitch.auth.start`, `twitch.auth.status`, `twitch.auth.disconnect`, `personality.get`, `personality.set` | shell, dashboard | `settings/install.py:341-351` |
| `home.status`, `home.list`, `home.light`, `home.switch`, `home.scene`, `home.test`, `home.command` | shell, dashboard | `home/ipc.py:137-143` |
| `clip.list`, `clip.tag`, `clip.export`, `clip.trim` | shell, dashboard | `clips/ipc.py:61-64` |
| `remote.pair.start`, `remote.devices.list`, `remote.unpair` | shell, dashboard (only when `remote.enabled`) | `remote/install.py:184-186` |
| Hub-internal: `ipc.auth` (handshake only), `ipc.ping`→`ipc.pong`, `ipc.subscribe` | any authenticated role | `ipc/server.py:696-705` |

Core→worker requests (handled in the worker): `tts.speak`, `tts.stop`, `stt.transcribe`, `voice.ptt`, `voice.mute` (`worker/main.py:173-178`). Core→plugin: `tool.call`, `plugin.stop` (`worker/plugin.py:86-87`). Inbound events from a worker or plugin are accepted only inside its declared `<service>.**` namespaces (`server.py:758-771`).

### 1b. CLI commands (`nox` = `nox.cli:main`, `pyproject.toml:66`)

`nox --version` · `nox core [--profile] [--no-voice] [--user-config]` · `nox shell` · `nox supervisor` · `nox dev [--no-voice] [--no-shell] [--profile]` · `nox doctor` · `nox onboard` · `nox rl calibrate` · `nox secrets set|delete|check <name>` · `nox voice download-kokoro|selftest` (or a stub that prints what is missing, `cli.py:168-191`). Module entry points: `python -m nox.app` (argparse mirror, `entrypoints.py:457-473`), `python -m nox.supervisor`, `python -m nox.worker [--service voice|stt|tts] [--plugin id] [--selftest] [--download-kokoro] [--models-dir] [--stt-model] [--tts-engine] [--hub-url]` (`worker/main.py:727-779`).

---

## 2. Architecture facts

**Process model.**
- The supervisor (`nox supervisor`) spawns the core (`python -m nox.app`) and the shell (`python -m nox.shell`). Both go into the supervisor's job object on Windows and get `NOX_PARENT_PID` on POSIX (`supervisor/main.py:379-403`).
- The core spawns the voice worker (`python -m nox.worker --service voice`) and plugin workers into its own job `nox-core-workers` (`app.py:169`, `boot/workers.py:116-135`).
- Control channel: newline-delimited JSON over TCP 127.0.0.1:47799, one shared token. The token goes to children in env `NOX_SUPERVISOR_TOKEN` and to the file `runtime/supervisor.token` (the shell reads the file at kill time: `shell/app.py:420`).
- Hub: WS on 127.0.0.1:47800 (+5 fallback ports). HTTP on 47801 (+5). Endpoints are published in `runtime/ipc.json`.

**Boot order** (`app.py:184-203`):
1. Directories + logging.
2. Supervisor client (heartbeat task).
3. `open_database` in a thread: full `PRAGMA integrity_check`, rename-aside on failure, migrate, load sqlite-vec.
4. Bus + state manager.
5. `restore_latest` checkpoint.
6. Security context + `verify_boot` (audit chain since checkpoint; a break means the kill switch and SAFE_MODE).
7. Tokens + request registry + core handlers + hub start + HTTP start.
8. Tool registry/executor + plugin manager (built, not started).
9. AI providers/router/ProviderCard.
10. HealthService: `run_once` then loop.
11. Pet, speech policy, session row, orchestrator.
12. Kill-switch hooks.
13. Stream services.
14. Voice worker spawn.
15. Extensions (`sensors, memory, health, proactive, pm, rl, clips, creative, home, settings, remote`; import in a thread, `install` on the loop).
16. `_started=True`, `system.started`, greeting task.
17. Plugins start + plugin health checks.

**Shutdown** (`app.py:525-570`): `system.level=stopping` → `system.stopping` → cancel core tasks (not awaited) → ProviderCard cancel → extensions (2 s each, reverse order) → orchestrator, stream_responder, funken_booking, stream_sessions, pet, health, plugins, supervisor client, http (≤10 s), hub (≤2 s close per client, sequential) → `workers.terminate_all` (2 s each) → final checkpoint + `system.stopped` audit + drain audit writer + session end → remove session token → DB close → job close.

**Restart/recovery.**
- Supervisor: core exit, 5 missed heartbeats (graceful `sup.kill mode=restart`, then up to 10 s wait), 10 missed (hard `kill_tree`). There is no backoff between respawns.
- More than `restart_limit` (3) in `restart_window_s` (300) → SAFE_MODE: core not respawned, shell respawned with `NOX_SAFE_MODE=1`, which no code reads.
- Shell respawned up to 3 per window.
- Plugins: respawned on process exit only, 3 per 300 s, with backoff (`plugins/manager.py:682-720`).
- Voice worker: never respawned except on `security.resume` (`handlers/core.py:313`, `app.py:624-627`).

**Reliability constants.**

| Constant | Value | Where |
|---|---|---|
| heartbeat interval | 2.0 s | `defaults.yaml:216` |
| boot_grace_s | 90 | `:217` |
| missed_for_graceful / hard | 5 / 10 | `:218-219` |
| kill_ack_timeout_s | 2.0 | `:220` |
| stop_timeout_s | 6.0 | `:221` |
| shutdown_grace_s | 10.0 (not configurable) | `supervisor/main.py:91` |
| supervisor AUTH_TIMEOUT_S / MAX_UNAUTHENTICATED_FRAMES | 5 s / 3 | `supervisor/main.py:63-64` |
| control MAX_LINE | 64 KiB | `messages.py:45` |
| supervisor-client reconnect | 1→10 s | `client.py:55-56` |
| hub auth timeout | 3 s | `server.py:123` |
| hub max frame | 1 MiB | `:124` |
| hub rate | 50/s, burst 200 | `:125-126` |
| hub send queue / event queue | 1000 / 1000 (overflow disconnects the client) | `:127-130` |
| hub request timeout | 10 s | `:131` |
| hub WS ping interval / timeout / close_timeout | 20 / 20 / 2 s | `:394-396` |
| worker token TTL | 60 s, single use | `tokens.py:33` |
| IpcClient backoff | 0.2→5 s; stops permanently on `auth.denied` | `client.py:143-144,412-421` |
| `tts.speak` timeout | 120 s | `boot/workers.py:62` |
| `tts.stop` timeout | 2 s | `boot/workers.py` |
| TERMINATE_GRACE_S | 2 s | `boot/workers.py:32` |
| kill-switch hook timeout | 2 s, run concurrently | `security/killswitch.py:69,166-183` |
| health interval | 30 s | `defaults.yaml:201` |
| health default check timeout | 5 s | `core/health.py:30` |
| provider health check timeout | 15 s | `boot/health.py:28` |
| ProviderCard budget | 3 s | `boot/ai.py:37` |
| provider HTTP timeout | 10 s (connect 5 s) | `boot/ai.py:41` |
| state checkpoint debounce | 5 s | `defaults.yaml:202` |
| checkpoints kept | 200 | `repos.py:55` |
| SQLite busy timeout | 5 s | `db.py:60,74` |
| extension stop budget | 2 s each | `boot/extensions.py:62` |
| HTTP stop wait | 10 s | `http.py:283` |
| HTTP startup timeout | 10 s | `http.py:243` |
| VOICE_READY_TIMEOUT_S | 90 | `app.py:109` |
| parent watch poll / exit grace | 1 s / 10 s | `parent_watch.py:34,37` |
| bus capacity | 10 000 | `bus.py:42` |

**IPC roles and permissions.**
- Session token: roles shell, pet and dashboard all authenticate with the same per-start token (`tokens.py:36,189-192`).
- Worker/plugin: one-time token bound to the client id (`tokens.py:197-211`).
- `core`, `supervisor` and `remote` cannot authenticate on the hub.
- A handler's own `roles` override `ROLE_ALLOWLIST` (`dispatch.py:172-188`). Every registered handler names roles, so `ROLE_ALLOWLIST` today only matters for worker/plugin service namespaces.
- Outbound redaction: `voice.transcript_*`, `ai.response_*`, `memory.*` and `chat.*` are withheld from pet, remote and plugin; `twitch.chat_message` from pet and remote; `security.audit` goes only to dashboard and shell (`server.py:84-94`).

**DB schema / migrations** (`data/migrations/`).

| Migration | Contents |
|---|---|
| 0001_initial | state_checkpoints, audit_log, sessions, turns, memory_items (+FTS5, triggers), vault_index, vault_chunks (+FTS5, triggers), health_history, temporary_grants, tasks, paired_devices |
| 0002_stream | stream_sessions, viewers, viewer_memory, chat_events, funken_ledger, moderation_actions, minigame_sessions |
| 0003_rl | rl_replays, rl_matches, rl_events |
| 0004_pm | pm_items |
| 0005_memory | vec_map, vault_note_versions |
| 0006_clips | clips, clip_markers |
| **0007** | missing (a gap in the numbering) |
| 0008_remote | 6× `ALTER TABLE paired_devices ADD COLUMN`, remote_pairings, remote_audit |
| 0009_vision | rl_vision_frames, rl_vision_analysis |
| 0010_notifications | proactive_notifications |

Tables created outside the migrations:
- `schema_migrations` (`db.py:167-170`)
- `memory_vec` (vec0, at runtime: `db.py:248-259`)
- `audit_log` again, plus the append-only triggers `audit_log_no_update` / `audit_log_no_delete` and `audit_verify_checkpoint` (`security/audit.py:44-69`)
- `pin_attempts` (`security/pin_attempts.py:29`)

The two `audit_log` definitions disagree: 0001 has `hash UNIQUE` and `result DEFAULT ''`, `audit.py:45-58` has neither.

---

## 3. Reliability findings

### Critical / High

**R-1 (High) — The voice worker can never reconnect after a hub disconnect, and nothing respawns it.**
- Worker tokens are single-use: `tokens.py:206` pops the token on first use.
- The voice worker's `IpcClient` reuses the original token with `reconnect=True` (`worker/main.py:596-603`). On reconnect the hub answers `auth.denied`, and `_reconnect_loop` gives up for good (`ipc/client.py:419-421`).
- The core only `detach`es (`boot/workers.py:146-158`, `app.py:717-720`). It respawns the worker only on `security.resume` (`handlers/core.py:313`).
- `test_client.py:108-130` asserts exactly this denial, while `tests/unit/voice/test_worker_reregister.py` "proves" re-registration using a `FakeIpcClient`. The feature is dead in production.
- **Scenario:** a 40 s loop stall (WS ping 20+20 s, `server.py:394-395`), a send-queue overflow (`server.py:816-825`) or an event backlog of 1000 (`:669-677`) drops the worker. Voice stays silent until the user restarts. Health then reports `voice: limited "worker starting"` forever, because the process is alive but `registered` is clear (`boot/health.py:58-69`). That is a misleading status.

**R-2 (High) — After any supervisor-routed kill, the watchdog is permanently disabled, even once the user resumes.**
- `kill_switch()` puts the supervisor into SAFE_MODE (`supervisor/main.py:316-317`). The hotkey always takes this path (`:765-768`).
- If the core acks, the core stays up. The user then resumes through the dashboard or shell (`security.resume` on the core, `handlers/core.py:290-314`), and **nobody sends `sup.resume`**: no code outside `supervisor/` references it.
- `_tick` returns early in SAFE_MODE (`:465-466`). From then on, core crashes and hangs are never restarted, and `status().kill_switch_engaged` stays true.
- The same trap applies after the restart limit: SAFE_MODE with no core, and no UI path to `sup.resume`. The shell ignores `NOX_SAFE_MODE` (set at `:386-389`, never read).

**R-3 (High) — The safe-mode gate on AI turns is cleared at boot, so chat and voice turns run in safe mode after a broken audit chain.**
- `_build_security` engages the kill switch (`app.py:291-296`) before the orchestrator subscribes, so the orchestrator never sees `security.kill_switch`.
- `_announce_started` then publishes `system.started` (`app.py:511-513`), and `Orchestrator._on_started` sets `_safe_mode=False` (`orchestrator.py:217-218`).
- `handle_text`'s only safe-mode guard (`:164-165`) is therefore open while `system.level=safe_mode`.

**R-4 (High) — The periodic health checks do expensive and blocking work every 30 s.**
- (a) `security.audit_chain` calls `audit.verify_chain()` synchronously on the event loop (`health/checks.py:95`, `security/audit_sink.py:110-111`). That is a full re-hash from genesis under the DB lock (`audit.py:278-280,298-316`), unlike `verify_since_checkpoint` at boot. The probe has no `await`, so the 5 s check timeout cannot interrupt it. The audit log is append-only by trigger (`audit.py:61`) and never purged, so the stall grows linearly forever. That means UI/TTS jank at first, then missed heartbeats and supervisor restarts.
- (b) `db` runs a full `PRAGMA integrity_check` every 30 s (`boot/health.py:44-49`) in a thread that holds `Database._lock` for the whole scan. Every synchronous DB call on the loop blocks meanwhile: checkpoints (`statemgr.py:169`), `health_history` inserts (`core/health.py:100`), Funken, audit-inline fallback. A timed-out probe leaves the thread running, and the next run starts another one.
- (c) `ai.claude_code` calls `provider.health()` directly (`boot/health.py:105-110`). That bypasses the router's health cache and the `cloud_allowed` privacy filter (`ai/router.py:271,293-295`). With the defaults (`health_roundtrip: true`, `defaults.yaml:144`), the core runs `claude --version` plus a real 1-token completion (`claude_code.py:304-329`) every 30 s, plus on every worker register, ready or disconnect (`handlers/core.py:402,426`, `app.py:720`). That is about 2,880 paid calls a day (the code's own estimate is $0.005 each), **including in `private`/`offline` privacy mode**. ClaudeCodeProvider has no privacy hook, which contradicts the hard rule "no outbound calls except … through the egress guard".

**R-5 (High) — A disconnected plugin worker becomes a zombie.**
- `run_plugin_worker` builds its `IpcClient` without reconnect (`worker/plugin.py:203-210`).
- On disconnect, `_stop` is never set. The process keeps running, and keeps its external connections (Twitch IRC, OBS, Home Assistant), while heartbeats fail forever (`worker/heartbeat.py:47-50`).
- The plugin manager restarts only on process exit (`plugins/manager.py:682-720`).
- `worker.heartbeat` is a no-op on the core (`handlers/core.py:429-430`). The heartbeat module's claim "The core marks a worker unavailable after three missed heartbeats" (`worker/heartbeat.py:3`) is false.

**R-6 (High, privacy honesty) — Retention is configured but never enforced.**
- No production code calls `TurnRepository.purge_expired` (`repos.py:241`), `purge_all_expired` (chat_events, viewers, viewer_memory: `stream_repos.py:520-534`), `HealthHistoryRepository.purge_older_than` (`repos.py:134`) or `TemporaryGrantRepository.purge` (`repos.py:431`). `memory/retention.py:3` explicitly declares turn retention "out of this job's scope".
- `security.audit.retention_days_security/normal` (`core/config/security.py:37-38`), `privacy.retention.metrics_days` and `viewer_data_inactive_months` are never read. Audit retention is impossible anyway, because of the append-only trigger (`audit.py:61`).
- `docs/PRIVACY.md:34` promises "Transcripts … retention window (7 days by default)". Transcripts and chat text are in fact kept forever.

**R-7 (High) — Database corruption handling misses the most common case.**
- `Database.__init__` issues `PRAGMA journal_mode=WAL` (`db.py:76`). On a damaged header this raises `sqlite3.DatabaseError: file is not a database` **[verified]**, before `open_database`'s rename-aside logic (`boot/persistence.py:29-35`).
- The boot fails, the supervisor restarts the core 3 times, then enters SAFE_MODE with no core, and there is no UI resume path (R-2).
- Also:
  - no backup is taken before `migrate()` (`persistence.py:36`), and `backups_dir` is unused;
  - migrations have no downgrade/unknown-id detection (`db.py:175-196`);
  - the 0007 gap means a later `0007_*.sql` would be applied **after** 0010 on existing DBs;
  - the renamed `.corrupt-*` file loses its `-wal`: SQLite discarded the stale WAL when the fresh DB was opened **[verified]**, so the most recent committed data is not preserved for forensics.

**R-8 (High) — A single bad key in user.yaml silently relaxes privacy and switches to a different database.**
- `loader.py:137-142` discards the **entire** user layer on any validation or YAML error, and the core boots on defaults: `privacy.mode: balanced` (`defaults.yaml:62`), `plugins.enabled: []`, `paths.data_dir: ${NOX_APP_DIR}`.
- A user who chose `offline`/`private` or a custom data_dir silently runs in `balanced` against an empty DB: new audit chain, no PIN lockout history, "lost" conversations. The only signals are a log warning and a `system.config: limited` health entry (`health/checks.py`).
- `settings/layers.py:176` writes user.yaml non-atomically (`path.write_text`), so a crash or disk-full mid-write produces exactly this corruption.

**R-9 (High) — A second `nox supervisor` breaks the running supervisor's tray kill path.**
- `start()` writes `supervisor.token` (`supervisor/main.py:254`) **before** binding the fixed control port (`:255-257`, no fallback).
- On `OSError`, `run()`'s `finally: stop()` unlinks the token file (`:305`), because state is STARTING, not STOPPED.
- The first supervisor's shell reads that file at kill time (`shell/app.py:420-430`), so the tray kill switch now fails with "no supervisor token".
- There is no single-instance guard anywhere. A grep for fcntl/msvcrt/mutex/pidfile finds nothing. A second `nox core` overwrites `session.token` and `ipc.json` too, and two processes appending to one audit chain collide on `seq` (process-local lock only, `audit.py:231-258`). The queued writer then logs and drops the entry (`audit_sink.py:142-160`).

### Medium

**R-10 — Shutdown can exceed the supervisor's 6 s budget, and the final records come last.**
- Ordered stop: extensions 2 s each, plugins, HTTP ≤10 s (`http.py:283`), hub closes clients sequentially at ≤2 s each (`server.py:425-426,849-854`), then workers 2 s each (`boot/workers.py:160-174`). Only after all that do the final checkpoint, the `system.stopped` audit entry and the session end run (`app.py:556-557,587-614`).
- The supervisor kills after `stop_timeout_s=6` (`supervisor/main.py:291-297`), so these records are routinely lost and sessions stay "active".

**R-11 — A failed boot leaks everything.**
- `stop()` returns immediately unless `_started` (`app.py:532-533`), which is only set in step 16 (`:510`).
- If the hub, HTTP, extensions or earlier steps fail, `_run` calls `stop()` (`entrypoints.py:284-294`), which does nothing. The heartbeat task, stale `session.token` and `ipc.json`, the open DB, the spawned voice worker (step 14) and the job handle all stay.
- The docstring's "a component that was never built is skipped" is not what the code does.

**R-12 — Losing TTS aborts the text answer.**
- `Orchestrator._run` awaits `speaker.say` per sentence with no try (`orchestrator.py:286-296`). `WorkerSpeaker.say` raises `IpcError` when the worker disconnects mid-turn, and blocks up to 120 s when it hangs (`boot/workers.py:53-63`).
- `chat.send` defaults to `speak=True` (`handlers/core.py:126`). A dashboard chat therefore errors out, and the assistant turn is never recorded, whenever voice is flaky.

**R-13 — Roles among session-token holders are self-asserted.**
- Any holder of the session token may claim `shell`, `pet` or `dashboard` (`tokens.py:189-192`). The pet page receives the token in its URL (`shell/logic.py:254`) and can be loaded as an OBS browser source.
- A pet or OBS page can therefore authenticate as `shell` and call `security.permission.reply` / `voice.ptt`. The "pet role is restricted" tests (`test_walking_skeleton.py:113`) hold only for a well-behaved client.

**R-14 — Turn history freezes after 1000 turns in one core session.**
- `DbTurnStore.recent` reads `list_for_session(…, 1000)` ordered `ORDER BY id` **ascending** with `LIMIT` (`repos.py:235-238`), then slices `[-limit:]` (`boot/persistence.py:72-73`).
- Once a long-running core passes 1000 turns, the model receives turns 993–1000 as "recent" context forever.

**R-15 — Checkpoint restore brings back transient and stale state.**
- `restore_latest` replaces the whole tree (`statemgr.py:185-208`): `system.started_at`, `system.health`, `voice.*` (`speaking`, `ptt_held`), `privacy.microphone`/`screen`/`panic`, `assistant.current_task`, `pet_functional`.
- Boot then overwrites only `privacy.mode` and `system.level` (`app.py:281-283`).
- Separately, the kill-switch/panic state is not persisted, so a security-path safe mode (panic or tamper; the PIN is required per `handlers/core.py:304`) is escaped by any core restart without a PIN. For a broken audit chain the check at next boot does re-trigger it.

**R-16 — Event-loop blocking in the health extension probes.**
- The `system.disk` and `memory.vault` probes do synchronous `exists()`/`iterdir()` on the loop (`health/checks.py:48,75-81`).
- `entrypoints.py:390-391` itself warns that a disconnected cloud-synced drive makes one `exists()` take seconds; the core's own `vault` check uses `to_thread` (`boot/health.py:55`).
- This is also a duplicate check (`vault` and `memory.vault`).

**R-17 — Worker, plugin and shell processes log with no PII filter and no file sink.**
- Only `app.py:209` and `supervisor/main.py:839` call `configure_logging`. The voice and plugin workers use structlog defaults: stdout, no `pii_filter`.
- The ENGINEERING.md "never log secrets" guarantee therefore does not hold in the processes that handle audio and third-party tokens, and their logs are lost when stdout is not a console.

**R-18 — Kill-switch worker hook races its own timeout.**
- `workers.terminate` (`app.py:444`) runs `terminate_all`: TERMINATE_GRACE_S=2.0 per worker, **sequentially** (`boot/workers.py:160-174`), inside a 2.0 s hook budget (`killswitch.py:176-183`).
- A slow worker makes the hook cancel before `process.kill()` and `self._workers.clear()`. `ensure_voice_worker` then sees `"voice" in workers` and never respawns it on resume (`app.py:624-627`).

**R-19 — The supervisor's graceful restart holds its lock for up to 10 s.**
- `_tick` awaits `_wait_exit(shutdown_grace_s=10)` under `self._lock` (`supervisor/main.py:467-483`). `kill_switch()` needs the same lock (`:314`), so the P1 hotkey kill can be delayed about 10–12 s during a graceful restart.
- There is also no backoff between core respawns (`:514-516`).

**R-20 — Unauthenticated `/health` leaks filesystem paths.**
- `/health` returns every component's reason (`http.py:164-165`, `app.py:684-699`), including `f"vault_dir missing: {vault_dir}"` and disk probe errors (`health/checks.py:52,76-81`).
- This contradicts the stated intent in `boot/health.py` (vault check comment: "where the user keeps their notes is not something an unauthenticated caller should learn").

**R-21 — A second concurrent `chat.send` leaves the first request unanswered.**
- `handle_text` cancels the in-flight task (`orchestrator.py:166-171`). The first caller's `await self._active` raises `CancelledError`, which `dispatch` does not catch (`dispatch.py:220-230`) and `_handle_request` re-raises (`server.py:715-716`). No reply is enqueued.
- This breaks the hub's "exactly one response" contract (`:693`), and the dashboard waits out its own timeout.

### Low

- **R-22:** `read_envelope` catches `asyncio.LimitOverrunError`, but `StreamReader.readline()` raises `ValueError` on overrun (`messages.py:88-91`). In `SupervisorClient._run` that escapes and permanently kills the heartbeat/reconnect task (`client.py:147-152` catches only OSError/ProtocolError). The supervisor's connection handler dies with an unretrieved exception.
- **R-23:** `SupervisorClient._authenticate` has no read timeout (`client.py:180`).
- **R-24:** `raw_transcripts_days: 0` is turned into `None` by `or None` (`app.py:426`, `persistence.py:56-60`), which means **keep forever**. For chat, `0` means "never store text" (`stream/sessions.py:112-117`). The same key value has opposite privacy semantics.
- **R-25:** Funken balance update, tier update and ledger append are three autocommit statements with no transaction (`stream/funken.py:155-163`). A crash between them desynchronises balance and ledger.
- **R-26:** `state.get` / `/api/state` with an unknown path raises `StatePathError`, which surfaces as `internal` or an HTTP 500 (`handlers/core.py:202`, `app.py:705-706`) instead of a validation error. `stream.funken.top.limit` is unbounded (`handlers/core.py:151-152`).
- **R-27:** `nox dev` does not catch `ConfigError` (`entrypoints.py:338`). `nox secrets *` does not catch `SecretStoreUnavailableError` (`cli.py:139-165`). `run_doctor` provider probes have no outer timeout (`entrypoints.py:415-418`). The dev shell is not in a job and gets no `NOX_PARENT_PID` (`entrypoints.py:327-333`).
- **R-28:** Two different glob dialects. The bus treats `**` as one-or-more segments and `*` alone as a catch-all (`bus.py:45-59`). The hub/client use zero-or-more with fnmatch (`globbing.py:84-101`). The recursive `**` matcher is exponential in the number of `**` segments across up to 64 client patterns of 96 characters (`server.py:153-162`); authenticated clients only.
- **R-29:** Bus fairness: a waiter is woken with `set_result(True)` but `in_flight` is incremented only when it resumes (`bus.py:177-191`). The capacity can briefly overshoot, and a woken-then-cancelled waiter loses the wake-up for the others.
- **R-30:** Hub request tasks per client are bounded only by the rate limit. At 50/s × a 120 s `chat.send`, one client can hold about 6000 in-flight tasks (`server.py:657-660`).
- **R-31:** `WorkerSupervisor.spawn` overwrites an existing entry without terminating the old process (`boot/workers.py:134`). `process.kill()` is not followed by `wait()` (`:171`).

---

## 4. Gaps and honest-status mismatches

- **No TODO/FIXME/NotImplementedError in scope** (grep). Dead or stub code: `events.py:1056` `_noop()`; `SupervisorClient.send_heartbeat_now`, `wait_connected`; `TokenStore.outstanding_worker_tokens`; `TaskRepository.list_by_status`; `HealthHistoryRepository.latest_per_component`. None of these has a caller in `src`.
- **Configured but inert:** `security.audit.retention_days_*`, `privacy.retention.metrics_days`, `privacy.retention.viewer_data_inactive_months`, `paths.backups_dir`, `paths.index_dir`, `paths.cache_dir`, `ipc.schema_version`, `ipc.auth` (the hub uses the `SCHEMA_VERSION` constant), `NOX_SAFE_MODE` (shell never reads it).
- **Handler roles naming unreachable callers:** the `supervisor` role on `privacy.set` and `security.*` (`handlers/core.py:176-179`).
- **Documentation and docstring claims the code does not meet:**
  - "transcripts retention 7 days" (`PRIVACY.md:34`): R-6.
  - "core marks a worker unavailable after three missed heartbeats" (`worker/heartbeat.py:3`): R-5.
  - "After the core dropped us … register again" (`worker/main.py:349-352`): impossible, R-1.
  - "relaunches the shell in safe mode" (`supervisor/main.py:18-19`): the env flag is ignored.
  - "leaving it needs the PIN" (`app.py:289`): a restart clears it, R-15.
  - "a component that was never built is skipped" (`app.py:20-22,528-531`): R-11.
  - "the router caches it" (`claude_code.py:280`): the health service bypasses the router, R-4c.
  - "database that fails its integrity check is renamed aside … so Nox starts" (`persistence.py` module docstring): not for header corruption, R-7.
  - README principle "recoverable by design (… rollback)": no DB backup or rollback exists.
- **Health honesty gaps:**
  - The voice check reports `limited "worker starting"` for a permanently disconnected worker.
  - The worker heartbeat is not tracked.
  - `ai.claude_code` probes ignore the privacy mode.
  - `system.config: limited` is the only sign that a user.yaml was discarded wholesale.

---

## 5. Extension points and fragile contracts

- **Extension contract** (`core/extension.py`): `nox.<name>.install.install(core)` runs on the loop and gets the whole `NoxCore`, a duck-typed `Any`. Extensions reach into `core.db`, `core.registry`, `core.bus`, `core.tool_registry`, `core.security.*`, `core.extensions`, … Any rename in `NoxCore` breaks extensions at runtime, not at type-check time. The order is hard-coded (`boot/extensions.py:30-42`), and a failure only logs.
- **IPC contract:**
  - Response models are validated only for names listed in `RESPONSE_PAYLOAD_MODELS` (`protocol.py:398-423`). Core handlers like `state.get`, `health.get`, `mode.set`, `privacy.set` and `security.*` return unvalidated dicts, and the TS types are generated only for the listed models.
  - Two role lists (`ROLE_ALLOWLIST` and per-handler `roles`) must be understood together (`dispatch.py:47-97,172-188`).
  - `client_version` major must equal the core major (`server.py:599-605`). Workers hard-code `"0.1.0"` (`worker/main.py:601`, `worker/plugin.py:208`), so a 1.0 core bump without touching those strings locks every worker out.
- **Event catalogue:** `validate_payload` passes unknown names through unchanged (`events.py:1048-1053`), so plugin-namespaced events are unvalidated. The bus and the hub disagree on glob semantics (R-28).
- **Shared SQLite connection:** every user must take `Database.lock`. `SqliteAuditLog`/`SqlitePinAttemptStore` receive the raw connection plus the lock (`app.py:274-275`), but nothing enforces the rule. A new component that uses `db.connection` without the lock, or calls `conn.commit()` inside another component's `transaction()` on the same thread (the lock is re-entrant, `db.py:65`), would silently commit or corrupt someone else's transaction.
- **Schema ownership is split** between migrations and `executescript` in the security modules (§2). `audit_log` has two conflicting definitions.
- **Migration numbering:** gap-tolerant, no checksum, no down-migrations, no "DB newer than code" check (`db.py:152-196`).
- **Worker tokens:** single-use and 60 s TTL is incompatible with any reconnecting client. Supporting reconnection needs a per-worker renewable credential, or a respawn policy on detach.
- **Supervisor ↔ core:** the core never tells the supervisor about a resume, and safe-mode state lives in two places (supervisor `_state` and core kill switch) with no reconciliation.

---

## 6. Test coverage map

| Module | Tests | Gaps / missing negative paths |
|---|---|---|
| `app.py` (boot/stop) | Integration via the `NoxCore` fixture (about 20 files) | No boot-failure/partial-boot stop test (R-11); no stop-budget test; periodic health disabled in fixtures (`check_interval_s: 3600`) |
| `core/boot/persistence.py` | **none** (`open_database`, `DbTurnStore`: 0 references) | Header corruption, rename-aside, WAL leftovers, >1000 turns |
| `core/boot/workers.py` | **none** (`WorkerSupervisor`, `terminate_all`: 0) | Crash/respawn, kill-hook timeout, double spawn |
| `core/boot/extensions.py` | **none** | Failing import/install/stop |
| `core/boot/health.py` | Indirect | Provider probe cost, privacy mode gating |
| `core/health.py` | 3 | Concurrent `run_once`, a synchronous probe exceeding its timeout |
| `health/*` | 14 | Loop blocking of `verify_chain` on a large chain |
| `supervisor/*` | 32 (fake core only) | Real core end-to-end; `resume` after dashboard resume (R-2); second instance (R-9); oversized line (R-22) |
| `ipc/server.py` | 33 + 2 | Slow-consumer and event-backlog disconnect (0 references), `TokenBucket` (0), `write_runtime_info` (0) |
| `ipc/client.py` | 8 | Session-role reconnect across core restart with a new token |
| `ipc/handlers/core.py` | Integration (`test_security_resume.py`, walking skeleton) | `worker.register` impersonation, concurrent `chat.send` (R-21), invalid `state.get` path |
| `ipc/tokens.py` | 13 | — |
| `data/db.py` | 11 | Header corruption, disk full (`SQLITE_FULL`), migration on a newer DB |
| `data/repos.py`, `stream_repos.py` | 19 | Purge jobs are tested but never scheduled |
| `worker/` | **no `tests/unit/worker/`**; `VoiceWorker` covered through voice tests with fakes | Real reconnect, plugin-worker disconnect behaviour |
| `util/aio.py`, `util/proc.py` | `poll_loop` 2 references; `wait_or_stop`, `find_binary`, `creation_flags`: 0 | — |
| `cli.py`, `entrypoints.py` | **0** (no test imports `nox.cli` / `run_doctor`) | Config error paths, doctor output |
| `core/statemgr.py` | 7 | Restore of transient fields |
| `core/orchestrator.py` | 15 | Speaker failure mid-turn (R-12); safe mode after boot-time kill (R-3) |
| `core/config` | 17 | Partial-invalid user.yaml, which today drops the whole layer (R-8) |

**Critical paths that lack negative tests:**
- DB unopenable at boot → restart loop → safe mode.
- Worker/plugin disconnect → recovery.
- Supervisor safe mode → resume through the UI.
- Health checks against a large audit log or DB.
- Privacy mode `offline` → no cloud health probes.
- Retention actually deleting data on a schedule.
