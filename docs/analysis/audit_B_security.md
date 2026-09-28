> **Provenance.** Machine-assisted subsystem audit of the tree between `6625b50` and `3af0a5a` (read-only, code-reading
> with a few scratch experiments), written for [`../CAPABILITY_ANALYSIS.md`](../CAPABILITY_ANALYSIS.md).
> Line numbers may be off by a few lines for files changed since. Findings that the consolidated analysis relies on were re-checked
> by hand and are marked there; treat everything else here as a well-sourced lead, not a verdict.

# Audit B: Nox security & privacy layer (defensive review, read-only)

Repo: the repository, working tree including uncommitted cross-platform changes. Nothing was modified or run apart from grep and read commands. The full test suite was not run. All line numbers refer to the current working tree.
Already-known items (shared audit DB lock; `sensors.enabled=false` disables zones; Windows click-through flag ordering) are not repeated here.

---

## 1. Guarantee verification table

| Guarantee | Where enforced | Verified? | Test evidence | Notes |
|---|---|---|---|---|
| Hard guarantees live below the LLM | `security/permissions.py:488` (`EVALUATION_GUARDS`), `tools/executor.py:145-160` | PARTIAL | `tests/unit/security/test_permissions.py` (20) | The orchestrator never lets the LLM call tools (no function calling). The only LLM→side-effect path is the stream responder (`stream/responder.py:112`, fixed tool `twitch.chat.send`). Several side effects skip the engine entirely: AI provider choice (see F1), creative screenshots (`creative/screenshot.py:122`, triggered by a plugin event with no PermissionRequest), and `plugin.tool.call`, which bypasses the executor (F4). |
| Hard prohibitions cannot be overridden | `security/hardlist.py`, `prohibitions.py:915`, `profiles.py:767`, config `effective_hard_prohibitions` | YES (name-level) | `test_prohibitions.py` (6), `test_profiles.py` | Matching is by name only. A tool that injects input under another name, such as `rl.pad.tap`, is not caught. The real barrier is the CI grep, which has gaps (F13). |
| Kill switch (engage, stop, resume) | `security/killswitch.py:379-501`, supervisor `supervisor/main.py:312-351`, hooks `app.py:440-447` | PARTIAL | `test_killswitch.py` (10), `tests/integration/test_security_resume.py` | Works in-process. Weak spots: the supervisor accepts a forged "core" (F6), kill and panic do not survive a core restart (F9), the orchestrator's safe-mode flag is event-driven and misses a boot-time safe mode (F8), and the egress guard ignores safe mode. |
| Kill switch reachable while the core is hung | supervisor hotkey → `sup.kill` → no ack → `kill_tree` (`supervisor/main.py:312-331`) | YES (design) / unproven in a real environment | supervisor unit tests with a fake core | Wayland has no hotkey, so tray→shell→supervisor is the only path there. The path can be defeated by core impersonation (F6). |
| Privacy modes FULL/BALANCED/PRIVATE/OFFLINE | `privacy.py:254-277`, `egress.py:171-217`, `permissions.py:_privacy_cloud` | PARTIAL | `test_privacy.py` (14), `test_egress.py` (9) | The core egress guard is correct for mode, apart from F2. Plugins' own guards start as BALANCED whatever the real mode is (F3). The mode is not persisted across restarts (F9). `state.privacy.mode` is stale after panic or a phone `/privacy` (Low). |
| Privacy zones incl. fail-closed `unobservable` | `privacy.py:311-344`, `sensors/foreground.py:99-121`, `permissions.py:391` | PARTIAL | `test_privacy.py` (includes `observe_foreground_unobservable`) | Zones are edge-triggered. Consumers spawned later default to "allowed" (F5). Only the *foreground* window is a zone, so full-monitor screenshots capture zoned windows that are visible but not in front (F11). |
| Permissions allow/confirm/deny | `permissions.py:762-879`, executor | YES (engine) / PARTIAL (callers) | `test_permissions.py`, `tests/unit/tools/*` | Plugin tools are registered with `targets=None` (`plugins/manager.py` `_register_tools`), so the confirm dialog cannot show *which* script or entity is involved. Grants rank above profile restrictions (F12). |
| Egress allow-lists per profile and plugin | core `egress.py`, `plugins/manifest.py:244`, `plugins/api.py:85-143` | PARTIAL | `test_egress.py`, `tests/unit/plugins/test_api.py` | "127." prefix bypass (F2). Plugin-side enforcement is cooperative, runs inside the plugin process, and is not audited (F4). The Claude Code CLI subprocess is outside the guard (F1). |
| Tamper-evident audit | `audit.py:211-340` | PARTIAL | `test_audit.py` (8): mutation and trigger-drop cases | Tail truncation, checkpoint rollback, DB deletion and full re-hash all go undetected (F7). Failed writes are swallowed (fail-open). `retention_*` and `audit.enabled` are dead config. |
| Secrets only in the OS credential store | `secrets.py:KeyringSecretStore`, `SecretStoreUnavailableError` | YES (Windows/macOS) / PARTIAL (Linux) | `test_secrets.py` (6) | No check of the keyring *backend* type: a `keyrings.alt` plaintext backend on Linux would be accepted silently. The PIN hash is PBKDF2 because argon2-cffi is not a dependency (F10). |
| PIN gate for relaxing changes | `gate.py:532`, `ipc/handlers/core.py:258-276`, `settings/editor.py:174`, `secrets_ipc.py:126` | NO in practice | `test_gate.py` (5), `test_secrets_ipc.py` | **No shipped code path sets a PIN** (F0), so every PIN gate is dormant on every real install. |
| Rocket League observation-only | CI `guard-security-model` (`ci.yml:138-161`), no input or memory APIs present | YES today / PARTIAL as a barrier | CI grep | Grep of the code: no `SendInput`/`pynput.Controller`/`OpenProcess` in `src/nox/rl` or `plugins/rl`. The guard's pattern list and scope are narrow (F13). The RL profile's "no egress/cloud" claim fails through F1. |
| Home Assistant hard boundary (no lock/alarm/valve/garage) | `home/boundary.py:require_allowed`, plugin `plugin.py:240` | PARTIAL | `tests/unit/plugins/home/*` | Domain-level only. `home.scene` is pre-approved (`companion.yaml` rule `companion.home.scene: allow`), and HA scenes can set `lock.*` state. A garage relay exposed as `switch.*`, or a `cover` without `device_class`, is controllable. Scripts and automations can call `lock.unlock` (confirm-gated, but the dialog does not show which script). Token sent in cleartext to `homeassistant.local` (F14). |

---

## 2. Security capability inventory

| Capability | Surface | Maturity |
|---|---|---|
| Permission engine (pure guards, confirm, session grants) | `DefaultPermissionEngine` | WORKS+TESTED |
| Tool executor (validate, check, confirm, kill-cancel, audit) | `ToolExecutor.call` | WORKS+TESTED |
| Confirmation reply | IPC `security.permission.reply` (role `shell`) | WORKS-UNPROVEN-IN-REAL-ENV (role is self-asserted, F6b) |
| Kill / panic / resume | IPC `security.kill`/`security.panic`/`security.resume` (shell, dashboard, supervisor); supervisor `sup.kill`/`sup.resume`/`sup.stop`/`sup.status`; voice kill phrase `voice.kill_phrase`; Telegram `/kill` | WORKS+TESTED (unit); hotkey WORKS-UNPROVEN-IN-REAL-ENV; Wayland LIMITED |
| Privacy mode and capture flags | IPC `privacy.set`; tray; Telegram `/privacy private\|offline` | WORKS+TESTED; not persisted (F9) |
| Privacy zones incl. `unobservable` | `PrivacyService.observe_foreground*`, `sensor.foreground_changed` | WORKS+TESTED (core); consumers LIMITED (F5) |
| Egress guard (core) | `EgressGuard.client()` used by AI providers, HA probe, Twitch OAuth | WORKS+TESTED; bypass F2 |
| Plugin egress guard | `PluginEgressGuard` (in the plugin process) | LIMITED (cooperative, unaudited, stale privacy) |
| Plugin manifest validation (namespace, secrets, hard list, egress vs profile) | `plugins/manifest.py` | WORKS+TESTED; event namespaces unreserved (F4) |
| Plugin secret access | IPC `plugin.secret.get` | WORKS+TESTED; the audit-failure-refusal claim is ineffective (F4e) |
| Plugin cross-tool calls | IPC `plugin.tool.call` | LIMITED/unsafe (F4) |
| Audit chain | `SqliteAuditLog`, `verify_since_checkpoint` at boot, `security.audit` events to dashboard | WORKS+TESTED for in-place edits; LIMITED for truncation (F7); no on-demand full verify over IPC (the health check calls `verify_chain` via `health/checks.py:95`) |
| Audit retention (`retention_days_*`), `audit.enabled` | config | MISSING (dead config) |
| Secrets store | CLI `nox secrets set/delete/check`; IPC `secrets.status/set/delete` (6 names, 10/min) | WORKS+TESTED |
| PIN set / verify / lockout (persisted) | `PinManager`, `SqlitePinAttemptStore` | Verify WORKS+TESTED; **Set MISSING** (F0); persistence of attempts untested |
| PIN status | IPC `security.pin.status` | WORKS (always `false` in practice) |
| Settings write path with allow-list | IPC `config.get/config.set` (`EDITABLE_PATHS`) | WORKS+TESTED; several security-relevant paths are not PIN-gated (F2 chain) |
| IPC hub authentication | WS `ipc.auth` (session token for shell/pet/dashboard; one-time worker tokens) | WORKS+TESTED; roles within the session token are not separated (F6b) |
| HTTP server | `/health` (no auth), `/api/state`, `/api/providers` (Bearer) | WORKS; no Host/Origin checks (Low) |
| Remote (Telegram) pairing and commands | `remote.pair.start`, `remote.unpair`, `remote.devices.list`; `/pair` 40-bit code, 5 min, rate-limited | WORKS+TESTED |
| Home boundary | `nox.home.boundary` | WORKS+TESTED at domain level; semantic gaps |
| Capture indicator | shell / pet `CaptureIndicator` | WORKS-UNPROVEN-IN-REAL-ENV |
| Temporary/session grants persistence | `RepositoryGrantStore` exists; `SecurityContext.build` uses `InMemoryGrantStore` | SCAFFOLDING (persistence unused) |
| `integrations_allowed` profile field | `model.py:87` | MISSING (never read anywhere) |
| Filesystem/git/shell tools referenced by profile rules | coding/companion/work YAML | SCAFFOLDING (no such tools registered) |
| Dependency vulnerability scan | RELEASE_CHECKLIST item 14 | MISSING (Dependabot version PRs only) |
| Signed installer/updates | EXTERNAL_SECURITY_REVIEW §1 mentions "signed installer once that work is complete" | MISSING |

---

## 3. Findings

Severity reflects impact under the stated threat model: a malicious or hallucinating AI layer, a malicious remote party, a malicious or compromised plugin, or another local process that has no token. Same-account admin malware is out of scope.

### F0: HIGH (honesty/control): the PIN can never be set, so every PIN protection is dormant
- **Where:** `PinManager.set_pin` (`security/secrets.py`) is only called from tests (`tests/unit/security/test_secrets.py`, `test_gate.py`, `tests/unit/settings/*`, `tests/integration/test_security_resume.py`). There is no call in `onboarding/wizard.py`, `cli.py` or `settings/*`, and no dashboard UI. `gate.py:443-445` says "`nox onboard` and the Settings page are where a PIN is set". Neither place does it.
- **Consequence:** `SecurityChangeGate.is_required()` returns False on every real install. Relaxing privacy (OFFLINE→FULL), re-enabling the mic or camera, editing `security.*`/`privacy.*`, changing stored credentials and resuming after a *security-path* kill (tamper, broken audit chain, panic) all go through with a click. Any holder of the session token can do all of it.
- **Worse:** `nox secrets set nox/security/pin` is accepted (`validate_name` matches) and stores the *raw* value. `is_set()` then becomes True, but `_verify` never matches an unprefixed value, so every gated change is permanently refused. The only recovery is deleting the keyring entry.
- **Fix:** add `nox pin set/clear` (a hidden prompt calling `set_pin`) and a wizard step. Refuse `nox/security/pin` in `nox secrets set`. Add a test that asserts some shipped entry point can set a PIN.

### F1: HIGH: profile `cloud_allowed: false` is not enforced for AI routing, and the Claude Code CLI runs outside the egress guard
- **Where:** `app.py:380` wires `cloud_allowed=self.security.privacy.allows_cloud`. `privacy.py:254-259` looks only at mode, panic and safe mode, never at the active profile. `ai/router.py:_cloud_block_reason` then reports "cloud blocked by profile" for a check that never consults the profile. The `ClaudeCodeProvider` (`ai/providers/claude_code.py:460`) is a subprocess that makes its own HTTPS connections, so it never passes the `EgressGuard`. `integrations_allowed` is not read anywhere.
- **Scenario:** the user is in the `work` profile ("work content never goes to the cloud… enforced by the permission engine and egress guard", `work.yaml:1-4`) with privacy BALANCED (the default). They paste proprietary code into chat. The router picks `claude_code`, and the code goes to Anthropic. The same happens in `rocket_league` (auto-switched by `rl/services.py:110`, "no egress of any kind").
- **Fix:** route through `lambda: privacy.allows_cloud() and engine.active_profile().cloud_allowed`, and filter providers by `integrations_allowed`. Add an integration test: work profile plus BALANCED means no cloud provider is ever selected.

### F2: HIGH (guard logic) / MEDIUM (exploitability): any hostname starting with "127." counts as loopback
- **Where:** `core/netloc.py:76`, `normalised.startswith("127.")`. In FULL/BALANCED, `egress.py:191-192` returns allow for every loopback host (`rule_id="loopback"`) before any allow-list is consulted.
- **Scenario:** someone holding the session token (a dashboard XSS, a browser extension, or another local user on Linux or macOS, see F6c) calls `config.set {"home.host": "127.evil.example"}`. That path is *not* PIN-gated because it is not under `security.`/`privacy.`. After the restart that config change needs, they press "Verbindung testen" (`home.test` → `home/probe.py:61`). The core's guarded client sends `Authorization: Bearer <HA long-lived token>` to the attacker. That token has full HA admin rights and can unlock doors directly, which defeats the Home Assistant boundary entirely. Any future URL-taking core tool (web fetch, provider base URL) inherits the same bypass.
- **Fix:** treat a host as loopback only when `ipaddress.ip_address(host).is_loopback` or it equals `localhost`. Better still, resolve the name and check the IP it resolves to (see §5).

### F3: MEDIUM-HIGH: plugins start with privacy BALANCED whatever the real mode is
- **Where:** `worker/plugin.py:76` `self.privacy = PrivacyView()` (defaults to BALANCED, `plugins/api.py:71`). The view is updated only by *later* `privacy.mode_changed` events. `plugin.register` does not return the current mode (`plugins/manager.py:798-824`), and `check_egress` at spawn ignores the privacy mode.
- **Scenario:** the user sets OFFLINE from the tray. They then switch mode to `stream` (`mode.set` → `apply_profile` spawns the twitch plugin) or a plugin crash-restarts. The twitch plugin's guard sees BALANCED and connects to `irc.chat.twitch.tv:6697`. The home plugin's own "refuse in private/offline" gate (PRIVACY.md "Privacy modes cut the connection") reads the same stale view and connects to HA. The OFFLINE promise breaks without any sign of it.
- **Fix:** return `privacy_mode` (and the capture flags) in the `plugin.register` response and apply them before `start()`. Refuse to spawn egress-declaring plugins in PRIVATE/OFFLINE.

### F4: MEDIUM-HIGH: plugin isolation is cooperative; the core does not enforce it
Plugins are separate processes, but the boundaries SECURITY.md lists are enforced *inside the plugin process* by `PluginApi` (`plugins/api.py`). Code in a plugin, or a compromised dependency of one, can bypass all of them:
- **a) Egress:** raw `socket`/`httpx` calls are not mediated. `PluginEgressGuard` is built with no audit log (`plugins/api.py:111-118`), so plugin network traffic never reaches the tamper-evident log, despite "every attempt is audited".
- **b) Listen scope:** `ipc.subscribe` accepts any pattern, including `**` (`ipc/server.py:745-756`). The server blocks only `REDACTED_FROM`. A plugin receives `sensor.foreground_changed` (every non-zoned window title), `security.permission_requested` (targets), `privacy.*` and more.
- **c) Emit scope:** `event_namespaces()` (`plugins/manifest.py:131`) grants whole namespaces and reserves none. A manifest with `emits: [privacy.capture_changed]` or `[system.started]` can forge `privacy.capture_changed {microphone:true}`. The voice worker obeys that without checking the source (`worker/main.py:267-270`), so the mic re-opens while a banking window is in front. A forged `system.started` clears the orchestrator's safe mode (`core/orchestrator.py` `_on_started`). The shipped RL manifest already gets the whole `game.**` namespace, and telegram gets `remote.**`, so it can forge phone commands from any `sender_id`.
- **d) Cross-tool calls:** `plugin.tool.call` (`plugins/manager.py:919-946`) runs *any* registered tool, core ones included (`memory.search`, `vault.read`), with `target=p.input.get("target")` chosen by the plugin (`:932`) instead of `spec.targets`. It skips the executor (no timeout, no kill cancellation, no outcome audit). Scenario: the twitch plugin calls `memory.search {"query":"passwort"}` and posts the result or sends it anywhere. Plugins are denied `memory.*` *events* precisely to prevent this.
- **e) Secret audit:** `_audit_secret`'s "refuse when it cannot be audited" (`manager.py:891-917`) never fires, because `self._audit` is a `QueuedAuditLog` whose `append` never raises.
- **Fix:** enforce the manifest's `listens`/`emits` *exact names* on the hub side, reserve core namespaces (`security`, `privacy`, `system`, `voice`, `ipc`, `memory`, `remote` unless explicitly granted), restrict `plugin.tool.call` to the caller's own namespace plus an explicit allow-list and route it through `ToolExecutor`, and check the event source in workers. Say in SECURITY.md that OS-level egress isolation (Windows Firewall per-exe rules or AppContainer) does not exist yet.

### F5: MEDIUM: capture state is edge-triggered; late consumers default to "allowed"
- **Voice worker:** `worker/main.py:80` `microphone_allowed: bool = True` and `killed=False`. The core never pushes the current `CaptureChanged` or safe-mode state on `worker.register` (`ipc/handlers/core.py:374-399` returns only the voice config). With `listening_mode: continuous` and `privacy.capture.microphone: false`, or a zone or `unobservable` active at spawn, the mic opens on every (re)spawn until some toggle happens.
- **RL plugin:** `plugins/rl/.../plugin.py:90` `CaptureGate(initially_allowed=True)` has the same problem for the screen (`privacy.capture.screen:false` is ignored until an event arrives).
- **Boot safe mode:** `app.py:200` `_start_voice_worker()` spawns the worker unconditionally even after `_build_security` engaged the kill switch for a broken audit chain (`app.py:283-293`). The worker never sees that kill event because it fired before the worker connected.
- **Fix:** include `effective_capture()`, the zone and `killswitch.is_engaged()` in the register responses and apply them before the pipeline starts. Default every gate to closed.

### F6: MEDIUM: role and identity binding on local control channels is weak
- **a) Supervisor "core" impersonation.** `supervisor/main.py:597-624` binds the core role to a *self-reported* `pid` in `sup.auth`. The peer socket is never tied to that pid. Anyone holding `NOX_SUPERVISOR_TOKEN` can claim `{"role":"core","pid":<core pid>}`: the shell; the voice worker and every core subprocess, since `core/boot/workers.py:120` passes `dict(os.environ)` and the Claude CLI inherits the env too; and `sup.status` even returns `core_pid`. Once accepted, the supervisor drops the real core's writer (`_close_core_writer`). The impostor then receives `sup.kill` from the hotkey and acks it, and the real core keeps running. It can also send heartbeats to hide a hang. The module docstring claims this exact attack is prevented.
  Fix: verify the peer pid from the socket (`psutil.net_connections` or `GetExtendedTcpTable` on the peer port), and strip `NOX_SUPERVISOR_TOKEN` from core child environments.
- **b) Session-token roles are self-asserted.** `ipc/tokens.py:36,187-192`: `shell`, `pet` and `dashboard` share one token and the client *chooses* its role. The pet renderer and the browser dashboard (`ui/shared/token.ts`) can therefore authenticate as `shell` and call `security.permission.reply` (`ipc/handlers/core.py:180`). A bug in the dashboard or pet page, or a browser extension on 127.0.0.1, can approve any pending confirmation. The "pet may only send ipc.*, pet.interact, state.get" boundary does not exist in practice.
  Fix: issue a separate token per role (the shell receives its own through the environment), and bind `security.permission.reply` to the shell's token.
- **c) Token on a process command line (Linux/macOS).** `shell/app.py:408` calls `webbrowser.open("http://127.0.0.1:47801/dashboard/#token=…")`, which becomes argv of `xdg-open` or the browser. Command lines are world-readable by default on Linux (`/proc/*/cmdline`) and visible through `ps` on macOS. Any other *local user* can read the token and connect to the loopback hub (loopback is shared between users) as `shell`. SECURITY.md says tokens are "never on a command line" only for workers.
  Fix: open the page through a one-time code exchange (for example a single-use nonce that the page swaps for the token over authenticated HTTP), or through a local file with 0600 permissions that redirects.
- **d) Audit actor spoofing:** `security.panic` passes `by=p.origin` unclamped (`ipc/handlers/core.py:287`), unlike `security.kill`. Low.

### F7: MEDIUM: the audit chain cannot detect truncation, rollback or re-hashing
- **Where:** `audit.py:282-296` and `_write_checkpoint` (`:328`).
- **Tail truncation:** `verify_since_checkpoint` starts at `checkpoint.seq+1`. If a process drops the triggers and deletes rows after the checkpoint, verification finds no rows, reports OK, and moves the checkpoint.
- **Rollback:** if rows at or below the checkpoint are deleted, `_verify_from` again finds nothing and `_write_checkpoint` moves the checkpoint *backwards* to the new last row. The rollback is accepted and hidden from then on.
- **Deleting `nox.db`:** yields a fresh valid chain. It also resets `pin_attempts`, i.e. the PIN lockout.
- **Full rewrite:** the hash has no key, so a rewrite with re-hashing passes `verify_chain_detailed`.
- **Loss and fail-open:** `SafeAuditLog` (`audit_sink.py:161-202`) swallows write failures, so actions proceed unaudited on disk-full or DB errors. `QueuedAuditLog` is a daemon thread, so up to 2048 queued entries are lost when the supervisor's `kill_tree` hits the core.
- **Private content:** targets such as `memory.search` query text (`memory/tools.py:61`) or `vault.read` paths are stored verbatim. With no retention implemented (`retention_days_*` is never read), private text in an append-only log is permanent. That contradicts "audit log never contains raw private content".
- **Threat-model note:** same-user tampering is formally out of scope, but SECURITY.md sells the chain as making "tampering detectable".
- **Fix:** refuse to lower the checkpoint and treat `last_seq < checkpoint.seq`, or a checkpoint hash that no longer matches its row, as a break. Anchor the head hash plus count outside the DB (in the keyring, and optionally HMAC entries with a keyring-held key). Make audit failure on side-effecting paths fail closed. Drop free-text targets.

### F8: MEDIUM: safe mode is partial when it is entered at boot or by event
- **Where:** `core/orchestrator.py` sets `_safe_mode` from `security.kill_switch` events and *clears* it on any `system.started` event. It is built after `_build_security` (`app.py` boot order), so a boot-time kill (broken audit chain) is never seen, and `_announce_started` then publishes `system.started`.
- **Consequence:** after a detected tamper, the voice worker is running (F5) and the orchestrator answers chat and voice with the local model. SECURITY.md says safe mode means "no side effects until manual resume". Background vault scanning (`memory/install.py` scan task) and the core-side Twitch token refresh keep going, and the egress guard does not consult safe mode (`egress.py:171`).
- **Fix:** have the orchestrator, the egress guard and the worker spawn read `killswitch.is_engaged()` directly instead of mirroring events.

### F9: MEDIUM: privacy mode, panic and kill state are lost on a core restart
- **Privacy mode:** `security/service.py:104` rebuilds privacy from *config* on every boot, and `app.py:281` overwrites the restored state. A PRIVATE/OFFLINE mode set from the tray or phone, or panic's forced OFFLINE, reverts to `balanced` after a watchdog restart (5 or 10 missed heartbeats) or a crash-respawn. That contradicts "Nox never silently becomes less private".
- **Kill switch:** the in-memory kill switch also resets. A dashboard-initiated kill or panic followed by a core crash comes back fully running without the PIN a security-path resume would need.
- **`sup.resume`:** from any supervisor-token holder, it respawns a fresh core (`supervisor/main.py:333-351`), which likewise clears a security-path kill without a PIN.
- **Fix:** persist `privacy.mode`, `panic` and `killswitch{engaged, security_path}` (the state checkpoints already mark these IMMEDIATE) and restore them before anything spawns.

### F10: MEDIUM-LOW: PIN brute force
Only relevant once F0 is fixed.
- **Race:** `verify_pin` (`secrets.py:240-268`) does `load()` → slow hash → `save(failed+1)` with no lock around it. N concurrent calls through `asyncio.to_thread` (up to 32 threads), sent via `security.resume`, `privacy.set` or `secrets.set` over several connections, all read `failed=0`. That gives about 32 guesses per "attempt" instead of 1.
- **Weak parameters:** `min_length=4` with no complexity rule. Lockout is a flat 15 minutes and resets fully, so about 480 guesses a day and a 4-digit PIN falls in 21 days or less.
- **Offline:** anyone who can read the keyring hash recovers a 4-digit PIN in milliseconds, whatever the KDF.
- **Docs mismatch:** argon2-cffi is not in `pyproject.toml` or `uv.lock`, so the PIN is PBKDF2 in practice. SECURITY.md says "Stored as an Argon2id hash".
- **Fix:** a single-flight lock around verification, escalating lockout, a 6+ digit minimum, and adding `argon2-cffi` to the dependencies.

### F11: MEDIUM: creative screenshots
- **Where:** `creative/screenshot.py:122-170`.
- **(a)** It captures the whole primary monitor (`sct.monitors[1]`). A password manager, email or chat window that is *visible but not in front* is captured, because zones only track the foreground window.
- **(b)** The capture is triggered by a plugin event (`creative.screenshot.requested`) with no PermissionRequest or confirm on the core side. The manifest's "medium, confirm" applies only when the plugin *tool* is called, so the plugin can request captures at will.
- **(c)** `app` comes from the plugin payload and is interpolated into the file name (`:165`), a path traversal (`app="../../x"`) that writes a PNG outside the runtime dir. Low impact.
- **Fix:** capture only the app's window rectangle; run the request through `ToolExecutor`/`engine.check` with the capture tool id; sanitise `app`.

### F12: LOW-MEDIUM: temporary grants rank above profile restrictions and survive profile switches
- **Where:** `permissions.py:495`. `_temporary_grant` runs before `_profile_cloud`, `_profile_tool_allowlist`, `_profile_filesystem_roots` and the profile rules.
- **Scenario:** a "remember" confirm in `coding` (`session:filesystem/write`, 8 h, any target) stays valid after `rl/services.py` or `mode.set` switches to `work` or `rocket_league`, so it overrides their roots and rules. Grants are not cleared on profile change.
- **Related:** a remembered confirm for a plugin tool covers *every* target, e.g. every HA script, because plugin tools carry no target.
- **Fix:** evaluate profile restrictions before grants, scope session grants to profile id plus target, and revoke session grants on `set_profile`.

### F13: LOW-MEDIUM: the CI "observation-only" guard is narrow
- **Where:** `ci.yml:156-157`. The pattern is `SendInput|pydirectinput|pyautogui|ReadProcessMemory|WriteProcessMemory`, scanning only `src/` and `plugins/rl/`.
- **Missing APIs:** `keybd_event`, `mouse_event`, `PostMessage`/`SendMessage` with `WM_KEY*`, `pynput...Controller` (pynput is already a `shell` dependency), `vgamepad`/ViGEm, `interception`, `pymem`, `OpenProcess`/`VirtualQueryEx`, `ctypes` name concatenation, and other plugins (creative, obs, any new one).
- **Required check:** whether it is a required status check cannot be verified from the repo. GitHub Actions are pinned by tag, not SHA.
- **Fix:** scan all of `plugins/`, switch from a grep to an AST/import allow-list, and add the APIs above.

### F14: LOW-MEDIUM: Home Assistant semantics and transport
- **Semantic gaps:** `home.scene` is allowed with no confirm (`companion.yaml`), yet HA scenes can include `lock.*` states. A garage relay is often `switch.*`, and a `cover` with no `device_class` is treated as a blind.
- **Transport:** the default `tls: false`, together with the allow-listed `homeassistant.local:8123` (mDNS), means a LAN attacker who spoofs mDNS receives the HA token in cleartext on connect. The same holds for the plugin's WS client.
- **Fix:** require TLS or an IP literal for non-loopback hosts, and make scenes that touch forbidden domains confirm, or resolve their members before activating them.

### F15: LOW: remaining items
- **Local HTTP:** no `Host`-header or `Origin` check on the HTTP server or WS hub (`ipc/http.py:186`, `ipc/server.py:519`). A DNS-rebinding page can read the unauthenticated `/health` (component states and reasons) and hold WS sockets open. Token auth blocks everything else. Fix: allow-list `Host: 127.0.0.1:<port>|localhost:<port>` and reject browser `Origin`s other than the served UI.
- **IRC line injection:** `send_privmsg` does not strip CR/LF (`plugins/twitch/.../irc_client.py:201`). The responder applies `splitlines()[0]`, but a direct `twitch.chat.send` from any tool caller can inject IRC lines. Fix: reject `\r\n` in `ChatSendInput`.
- **Prompt injection via Twitch:** chat text reaches `twitch.chat.send` as the broadcaster account through `stream/responder.py`. The reply can start with `!` and trigger mod-only commands of other bots (Nightbot `!addcom` and similar). The moderation gate does not filter a leading `!` or `/`. Fix: strip or prefix the leading command sigil.
- **Stale UI mode:** `state.privacy.mode` is not updated on panic or phone `/privacy` (only `ipc/handlers/core.py:255` and boot write it). The UI shows the old mode, which errs on the safe side.
- **Unpaired Telegram senders:** every message from an unpaired sender is audited, and the audit log has no retention. That allows unbounded DB growth (a DoS through the audit log).
- **Silent plaintext secrets:** `keyring` backend type is not checked, so a Linux install with `keyrings.alt` would store secrets in plaintext silently. Fix: refuse `PlaintextKeyring`/`fail.Keyring` by class name and report `unavailable`.
- **Coding plugin:** the plugin (`plugins/coding/.../session.py`) grants Claude Code `Write` with `acceptEdits` inside the workspace. A prompt-injected session, e.g. through a README in a cloned repo, could write `.git/hooks/*` or `.vscode/tasks.json`, which runs code on the user's next commit or open. Whether Claude Code blocks writes into `.git` or across symlinks and junctions was not verified. Fix: deny `.git/**` and IDE task files, and resolve symlinks.
- **Kill cancellation:** handlers that run in threads, such as `asyncio.to_thread`, are not interrupted by the executor's cancel (`tools/executor.py:273`).

---

## 4. Honest-status mismatches (docs vs code)

| Claim (doc) | Reality |
|---|---|
| PIN "Stored as an Argon2id hash"; protects relaxing changes, security-path resume, "audit-log reset" (SECURITY.md, PRIVACY.md) | No way to set a PIN (F0). PBKDF2 in practice (argon2-cffi is not a dependency). No audit-reset feature exists. |
| "`nox onboard` and the Settings page are where a PIN is set" (`gate.py` docstring) | Neither does it. |
| work profile: "Local models only… enforced by the permission engine and egress guard"; rocket_league "no egress" | Claude Code is used whenever privacy is BALANCED or FULL (F1). |
| "`integrations_allowed`" is a profile control (SECURITY.md permission model) | Never read. |
| "Every attempt is audited" (egress); "Logged: … every security decision… plugin lifecycle" | Plugin egress is not audited. Audit write failures are swallowed. Queued entries are lost on a hard kill. |
| "Tampering is detectable"; "verify_chain() runs at boot" | Boot runs an incremental check that accepts truncation and rollback (F7). There is no user-triggered full verify over IPC. |
| "audit log never contains raw private content" | Query and path targets are stored verbatim, permanently (no retention). |
| Plugin boundary: "emit/listen to the exact event names its manifest lists… Nothing a plugin does can widen what the core already validated" | Enforced in the plugin process only. Namespaces are granted. Subscriptions are unrestricted. `plugin.tool.call` reaches core tools (F4). |
| "every connecting client is assigned a role… not just authenticated = trusted" | Session-token clients pick their own role (F6b). |
| Supervisor: the core role is "bound to the process the supervisor actually spawned" | Bound to a self-reported pid (F6a). |
| Kill switch effect: "capture turns off, memory writes stop… no side effects until manual resume"; "Recovery only by explicit manual restart" | Boot safe mode leaves the orchestrator and voice running (F8). State is lost on a crash or watchdog restart (F9). |
| PRIVACY: "Nox never silently becomes less private" | A restart reverts to the config mode (F9). A plugin spawned during OFFLINE behaves as BALANCED (F3). |
| PRIVACY: zones are windows Nox "should never look at… no capture, no screenshot" | True only for the foreground window. A full-monitor screenshot includes visible zoned windows (F11). |
| PRIVACY: "Privacy modes cut the connection" (home) | The plugin's check reads a stale BALANCED view (F3). |
| `SECURITY.md` root: "we track dependency updates via Dependabot" (true) vs RELEASE_CHECKLIST 14 vulnerability scan | Version updates only. No `pip-audit`/`npm audit`/OSV gate. |
| `security.audit.retention_days_*`, `security.audit.enabled` in defaults.yaml | Dead config. |
| Secret audit: "a secret handed to a plugin with no trace… is refused" (`manager.py:891`) | Cannot trigger with `QueuedAuditLog`. |
| Coding/companion/work rules for `filesystem*`, `git*`, `shell*` | No such tools exist. The rules are scaffolding and cannot be exercised. |

---

## 5. What a reliable future version needs (prioritised)

1. **Make the PIN real (F0):** CLI and wizard set/clear, Argon2id as a hard dependency, a 6+ digit minimum, an atomic attempt counter, escalating lockout. Without this, the security-change gate protects nothing.
2. **One authoritative egress/cloud decision:** profile `cloud_allowed` and `integrations_allowed` in the router. Put every subprocess that reaches the network (the Claude CLI) behind the same policy object. Fix `is_loopback` (IP-based). Resolve names and pin the IP (anti-rebinding). Consider an OS-level backstop (Windows Firewall rules per Nox executable or AppContainer for plugins) so that enforcement stops being cooperative.
3. **Level-triggered security state:** a single `SecuritySnapshot` (privacy mode, capture flags, zone, safe mode, panic) handed to every worker and plugin at register time and re-sent on reconnect. Persisted across restarts, and every gate closed by default.
4. **A server-side plugin boundary:** exact-name `emits`/`listens` enforced by the hub, reserved core namespaces, `plugin.tool.call` limited to the plugin's own namespace and routed through `ToolExecutor`, source checks in workers, and plugin egress audited (the plugin reports each attempt to the core, or better, egress goes through a core-side proxy).
5. **Identity-bound local IPC:** per-role tokens, a peer-pid check on the supervisor channel, no supervisor token in grandchild environments, no token on command lines, and `Host`/`Origin` checks.
6. **Audit hardening:** a monotonic checkpoint, a head anchor in the keyring (plus optional HMAC), fail-closed audit on side-effecting paths, an on-demand full verify over IPC, real retention that respects the chain (segment plus signed summary), and no free text in targets.
7. **Dependency vulnerability scanning (RELEASE_CHECKLIST 14):** `pip-audit` against `uv.lock`, `npm audit --omit=dev` for `ui/*`, or OSV-Scanner, as a required CI job with a documented exceptions file. Pin GitHub Actions by SHA. Generate an SBOM (CycloneDX) per release.
8. **Signed releases and updates:** Authenticode-signed installer and binaries, signed update manifests with rollback protection, and reproducible build notes. Plugin packages should be signed or hash-pinned before any third-party plugin exists.
9. **External review (EXTERNAL_SECURITY_REVIEW.md §8):** still unassigned. Hand the reviewer this report's F0–F9 as known issues and ask for attacks on the IPC, supervisor and plugin boundary first.
10. **Broaden the CI observation-only guard:** AST/import allow-list, all `plugins/`, and the API list from F13. Make it a *required* status check.
11. **Home:** TLS or pinned-IP requirement for non-loopback HA, scene member inspection, entity-level confirm text.
12. **Threat-model doc update:** state clearly that same-user malware, other local users on POSIX, and plugin supply chain are (or are not) in scope, and align the claims in §4 accordingly.

---

## 6. Security test coverage gaps

The existing tests cover the pure engine, egress-mode matrix, profile validation, prohibitions, audit mutation after a trigger drop, the kill switch in-process, the gate (with a PIN injected), and the secret-store-unavailable path. Missing:

**Audit**
- No test for tail truncation, deleting rows at or below the checkpoint (rollback), a checkpoint moving backwards, DB replacement, or a full re-hash. `verify_since_checkpoint` has **zero** tests.
- No test that an audit write failure blocks (or at least flags) a side effect.

**PIN**
- `SqlitePinAttemptStore` has **no** test for persistence across a "restart" (a new `PinManager` on the same connection).
- No concurrent `verify_pin_async` test (the race in F10).
- No test that any shipped entry point can set a PIN (F0).

**Egress and routing**
- No test for `is_loopback("127.evil.com")` or `localhost.evil.com`, nor for hostname-versus-IP handling.
- No router-wiring test with the real `app.py` callable under `work`/`rocket_league`. The router test passes `cloud_allowed=False` directly and so misses F1.

**Plugins**
- No test that a plugin cannot subscribe to `**` or to unlisted events.
- No test that a plugin cannot emit into `privacy.*`/`security.*`/`system.*`.
- No test that a plugin cannot `plugin.tool.call` a core tool or spoof `target`.
- No test that the plugin `PrivacyView` matches the core mode at spawn.
- No test that plugin egress is audited.

**Capture and safe mode**
- No voice-worker test for the initial state (mic closed until the core says otherwise), nor for spawning under boot safe mode.
- No RL gate test with `screen:false` at start.
- No test that the orchestrator refuses turns after a boot-time kill, or after a forged `system.started`.
- No test for persistence of privacy mode, panic or kill state across a restart.

**IPC and supervisor**
- No test of a session-token client claiming `shell` from the pet or dashboard.
- No test of a forged `pid` in `sup.auth` (existing tests use a wrong pid and do not cover a *claimed correct* pid from a different peer).
- No DNS-rebinding test (`Host`/`Origin`).

**Other**
- No negative test for full-monitor capture while a non-foreground zone window is visible, nor for `app` path traversal in creative screenshots.
- No IRC CR/LF injection test for `twitch.chat.send`, and no test for a leading `!` or `/` in responder output.
- No symlink or junction escape tests anywhere (vault `_resolve_within` resolves first, which is good but untested; coding workspace symlinks are untested).
- No HA boundary tests for a scene containing locks, a `switch.*` garage, or a cover without `device_class`.
- The EXTERNAL_SECURITY_REVIEW §4 "negative suite green" run output is not in the repo. The obligation is stated, but no evidence artifact exists.
