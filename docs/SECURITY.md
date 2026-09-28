# Security overview

Audience: technical users and the external security reviewer (see `EXTERNAL_SECURITY_REVIEW.md`).
This is a summary of the project's internal Security Model (the authoritative source —
if this document and the Security Model disagree, the Security Model wins) plus
`docs/ENGINEERING.md`'s hard rules. Security guarantees in Nox are enforced in code, below the AI
layer, never as prompt instructions to the model (Security Model principle P1).

## Reporting a vulnerability

**Never in a public issue.** Use GitHub's private vulnerability reporting (repository → Security
tab → "Report a vulnerability"). The full policy — supported versions, what to include, response
targets and the 90-day coordinated-disclosure window — is in the repository's root
[`SECURITY.md`](../SECURITY.md). This is a solo/small-project release: there is no funded bug-bounty
program and no guaranteed response SLA, but security reports get priority triage over feature
requests.

## Threat model

| Threat | Control |
|---|---|
| LLM proposes a harmful/unwanted action (hallucination, prompt injection via chat/files/web) | Tool pipeline with permission checks; untrusted input is data, never free-form execution; medium+ risk actions require confirmation. |
| Cloud leakage of private content | Privacy modes/zones enforced at the transport level; egress allow-list per profile; screenshots to cloud only in FULL and outside zones. |
| A local process talks to the Nox hub | Loopback-only IPC, per-client tokens, roles, rate limits, a request allow-list per role. |
| A web page talks to the Nox hub or HTTP server (DNS rebinding, cross-site WebSocket) | Both servers accept only a `Host` naming this machine on their own port, and a browser `Origin` only from the core's own pages; everything else gets 403 before a handler or the token check runs. |
| Secrets in code/config/logs/prompts | Keyring-only storage; secrets are never serialized; a log/PII filter; the prompt builder excludes the secret store entirely. |
| Stream accidents (wrong scene, leaking screen, stream stopped by mistake) | Hard prohibitions (`stream.stop`, `stream.key.read`); scene switches require confirmation in the stream profile; privacy zones hide windows from capture. |
| Anti-cheat / game integrity | No input-synthesis, no process injection, no memory reads exist anywhere in the codebase; hard prohibitions; sensors are observation-only. |
| A runaway or hung AI | Kill switch lives below the AI layer, in the supervisor; panic mode; budget limits. |
| Tampering with audit or security config | Hash-chain verified at boot, with the head anchored outside the database so deleted rows or a replaced database are detected too; changes to the security core require a PIN; config changes are themselves audited. |
| Data loss | Checkpoints; the vault is the source of truth; Nox-written notes can be rolled back. |

**Out of scope**: an attacker with local admin on the machine, physical access, or a compromised
OS. Nox defends against a malicious/hallucinating AI layer and a malicious remote party — not
against someone who already owns your Windows account.

## Permission model

Every tool call goes through the permission engine (`nox.security.permissions`) before it runs.
Evaluation order, first match wins:

1. **Hard prohibitions** — unconditional deny (see below); nothing overrides this.
2. **Kill switch / safe mode** — deny all side effects.
3. **Privacy mode constraints** — e.g. cloud tools denied in PRIVATE/OFFLINE.
4. **Temporary grants** — an explicit, time-boxed allow the user gave.
5. **Profile rules** — first match wins (`companion`, `coding`, `stream`, `research`, `work`,
   `offline` — each profile declares its own `cloud_allowed`, `egress_allowlist`,
   `memory_writes_allowed`, `screenshots_to_cloud`, `filesystem_roots`, `tools_allowed`,
   `integrations_allowed`).
6. **Default by risk**: `read` → allow, `low` → allow, `medium` → ask for confirmation,
   `high` → ask for confirmation, `critical` → deny.

Every decision — allowed or denied — is audited with the request fields (redacted per privacy
class where appropriate). The engine is pure and deterministic given (request, profile, privacy
state, grants, time), which makes it fully unit-testable, including every negative path.

### Decisions that are not tool calls

Which language model answers, whether a turn or a memory item is stored, and whether a plugin may
start are not tool calls, so the engine never sees them. They are decided in one place,
`nox.security.policy.EffectivePolicy`, which combines the *active* profile's `cloud_allowed`,
`memory_writes_allowed` and `integrations_allowed` with the live privacy state (mode, zones, panic,
kill switch). The router, the Claude Code provider (which connects to the cloud outside the egress
guard and therefore refuses to start at all while the cloud is blocked), the orchestrator's turn
history, the memory service, the vault writer and the plugin manager all ask it and nothing else.
It reads both inputs on every question, so a profile switch or a privacy change applies without a
restart; the plugin manager re-evaluates on both events and stops or starts plugins accordingly.
A plugin that reaches a host beyond this machine does not run in PRIVATE or OFFLINE. A test
matrix (every shipped profile × every privacy mode × chat, escalation, memory write, plugin start)
pins the outcomes down.

## Hard prohibitions

Never overridable by any profile, plugin, runtime override, or the AI/LLM layer — a config
attempting to remove one of these fails validation; the list may only be extended, never shrunk:

```
game.input.send
game.memory.read
game.process.inject
anticheat.bypass
stream.key.read
stream.stop
recording.delete
security.core.modify_without_pin
permission.self_elevate
```

## Kill switch and panic mode

- **Trigger points**: the supervisor hotkey `ctrl+alt+shift+k` (works even if the core has hung —
  this path does not depend on the core process; not available under a Linux Wayland session,
  which forbids global hotkeys, and on macOS only with the Input Monitoring permission - the
  supervisor reports `hotkey: unavailable: <reason>` instead of claiming it is armed; under Wayland
  the spoken phrase works only with continuous listening, because push-to-talk is a hotkey too,
  and not at all under `privacy.unobservable_policy: strict`), the tray, the dashboard, the pet's
  own menu, and the spoken phrase "Nox, Notaus" (matched locally by the STT worker and forwarded as
  a `security.kill` request — it never goes through the LLM).
- **Effect**: SAFE_MODE. Every in-flight AI request is cancelled, TTS stops in under 200 ms,
  workers/plugins stop, capture turns off, memory writes stop, the event is audited, and the pet
  shows `unavailable`.
- **Resume** always requires an explicit user action, and only from a user-controlled path — the
  supervisor (tray/hotkey), the shell, or the dashboard (IPC roles `supervisor`, `shell`,
  `dashboard`). A resume request from `pet`, `worker`, `plugin`, `remote`, or anything originating
  in the AI layer is rejected outright.
- **PIN requirement on resume**: only when the *security layer itself* triggered the kill —
  tamper detection, an audit-chain break, panic mode, or supervisor tamper. A user-initiated kill
  (hotkey, tray, voice, shell, dashboard) resumes without a PIN. Every engage and resume, allowed
  or denied, is audited with its origin and whether a security-path PIN was required.
- **Where resuming happens**: the dashboard's Status page ("Fortsetzen", with a PIN field when the
  core asks for one) and the tray menu ("Fortsetzen") both send `security.resume` to the core,
  which is the only place that checks the PIN. Once the core has let go of the kill switch, it
  tells the supervisor over its own authenticated control connection (`sup.resume {rearm: true}`),
  so the watchdog that a hotkey or tray kill put into safe mode restarts a crashed core again; the
  core is not restarted for this. The supervisor accepts that re-arm only from the spawned core's
  connection. A `sup.resume` from any other client (the tray while the core is unreachable) is
  honoured only while no core is connected - after the restart limit, or after a kill the core
  never acknowledged - and then starts a fresh core; while a core is connected it is refused with
  `core_running`, because restarting the core from outside would step around its PIN check.
- **Panic mode** = kill switch + privacy forced to OFFLINE + pet hidden + stream-safe behavior
  (an OBS privacy-scene request in v0.2+). Panic never deletes anything.
- **Survives a restart**: the privacy mode, panic and the kill switch (engaged, its origin and
  whether it came from the security path) are written to the database on every change - a move to
  a stricter mode before it is announced - and restored at boot before anything can act. A core
  that crashes or is restarted by the watchdog comes back in the same mode and, if the kill switch
  was engaged, in safe mode; leaving it takes the normal resume path, PIN included after a
  security-path kill. If the stored state cannot be read, the boot uses the strictest of the
  stored and the configured privacy mode and starts in safe mode (origin `state`, security path).
- **Every consumer starts from the current state**: a worker or plugin that registers receives the
  privacy mode, the effective capture flags and whether the kill switch is engaged, and applies
  them before it does anything; events only carry changes after that. A plugin whose kill switch
  is already engaged is not started, and a plugin worker that has not heard from the core yet
  treats everything as closed (OFFLINE, no capture). The orchestrator, the egress guard (safe mode
  allows only allow-listed loopback services) and the pet read the kill switch directly instead of
  mirroring events, so a kill engaged at boot is never lifted by the boot's own `system.started`.

## Rocket League observation-only boundary

Immutable, code-level, not a setting: screen/HUD capture, game audio, replay files, and
non-invasive input *observation* (hotkey-listener statistics, never sending input) are the only
things Nox's Rocket League feature does. No file anywhere in the repository may import an
input-synthesis API (`SendInput`, `pydirectinput`, `pyautogui`) or a process-memory API
(`ReadProcessMemory`, `WriteProcessMemory`) for a game window. This is enforced by CI, not just by
convention: the `guard-security-model` job in `.github/workflows/ci.yml` greps all of `src/` for
these patterns on every push and pull request and fails the build the moment one appears, no
matter which module introduced it.

## Secrets handling

`SecretStore`, backed by the `keyring` package (Windows Credential Manager, macOS Keychain, or a
Linux Secret Service such as GNOME Keyring or KWallet), names shaped `nox/<component>/<key>` (e.g. `nox/twitch/oauth_token`,
`nox/obs/websocket_password`). Set via `nox secrets set <name>` (value read from a hidden prompt,
never a command-line argument) or the onboarding wizard's equivalent prompts; a dashboard UI is
planned. Values are never logged, never included in a `repr()`, never audited, and never placed in
a prompt sent to an AI provider — the prompt builder excludes the secret store entirely. Plugins
only ever see the exact `nox/<their-id>/...` names their own manifest declares (see
`PLUGIN_AUTHORING.md`); the core reads the keyring on their behalf, so a plugin process never holds
a persistent handle to the store itself. Automated tests use an in-memory backend and never touch
the real credential store or a real credential (a suite-wide fixture enforces it).

When the credential store cannot be reached at all (a Linux session without a Secret Service), a
read raises `SecretStoreUnavailableError` - it is never turned into "no such secret". Nox still
boots, the `secrets` health check reports `unavailable` with the reason, and because it can no
longer tell whether a PIN is set, every change that would relax security is refused until the
store is back.

## Audit logging

Append-only, hash-chained (`prev_hash`/`hash`, SHA-256 over the canonical JSON of each entry plus
the previous hash). Logged: every security decision, mode/privacy change, kill/panic event, config
change, tool execution with side effects, memory deletion, and plugin lifecycle event. The audit
log itself is designed to never contain secrets or raw private content.

**Complete, not only consistent.** A hash chain shows that the rows it has belong together; it
cannot show that rows are missing. The boot check therefore also looks backwards:

- **Head anchor outside the database.** The last sequence number and its hash are kept in a small
  file in the runtime folder (`audit_head-<digest of the database path>.json`, replaced atomically
  after every row) and in the credential store (`nox/security/audit_head`, written at boot and at
  shutdown). A database whose head is behind an anchor (rows deleted from the end), a database
  that is new while an anchor exists (the file was deleted or replaced), or an anchored row that
  now has another hash is a broken chain. An anchor that exists but cannot be read counts as
  broken too. No anchor at all is a fresh install and passes. On a machine without a credential
  store the file anchor is the only one, and the boot log says so
  (`security.audit_anchor_unavailable`).
- **Monotonic checkpoint.** Boot verification walks forward from the last verified `(seq, hash)`
  checkpoint so a long log does not make every start a full scan. The checkpoint only ever moves
  forward; a head behind it, or a checkpoint row whose hash changed, is a rollback.
- **A break engages safe mode** (origin `audit`, security path: resuming needs the PIN when one is
  set), with an `audit.verify` entry that names the reason (`chain`, `truncated`, `deleted`,
  `rollback`, `rewritten`, `anchor_unreadable`). Neither the checkpoint nor an anchor moves past a
  break, so every boot finds it again until a person resumes: the resume records an
  `audit.break_acknowledged` entry and anchors the chain afresh from its current head.
- **No side effect without a record.** The entry that allows a tool with side effects to run, and
  the entry for every outbound HTTP request, is committed *before* the action; when the audit log
  cannot take it (a full disk, a broken database) the tool call is refused with
  `audit.unavailable` and the request with the egress rule `audit.unavailable`. Records of
  decisions and outcomes stay best effort: a failure there is logged, never silently dropped.

What this does not cover: the chain is unkeyed, so someone who can rewrite the database, the
runtime folder and the credential store together - as the same user - can produce a consistent
forgery; that is inside the "already owns your account" boundary above. The anchor file is not
flushed to disk on every row (the database is not either), so after a power cut the log can in
rare cases look truncated; Nox then starts in safe mode once, and the resume acknowledges it.
Raw-socket connections that plugins open themselves are checked by the plugin's own guard but not
audited in the core yet.

## PIN

Protects: relaxing the privacy mode or switching a capture device back on, edits to `security.*`
and `privacy.*` settings from the dashboard, changing a stored credential, and resuming after a
*security-path* kill (see "Kill switch" above — a user-initiated kill needs no PIN). There is no
audit-log reset feature for it to protect. Without a PIN set, none of these ask; with the
credential store unreadable, all of them are refused.

- **Setting it**: `nox onboard` (optional step, hidden prompt, typed twice), `nox pin set` /
  `nox pin clear` / `nox pin status`, or Settings → "Sicherheits-PIN" in the dashboard
  (`security.pin.set` / `security.pin.clear`, UI roles only, 5 changes per minute, every refusal
  audited). All three follow one set of rules (`nox.security.pin_setup`): the first PIN needs
  nothing, changing or removing it needs the current one, and a PIN has 6 to 64 characters.
- **Storage**: an Argon2id hash in the keyring under `nox/security/pin` (`argon2-cffi` is a
  dependency). A PBKDF2-SHA256 hash written by an earlier version is still verified and is
  re-hashed to Argon2id on its next successful verification. The PIN itself, its hash and its
  length never leave the core; `security.pin.status` reports only whether one is set and which
  changes will ask for it.
- **A raw value is never a PIN**: `nox secrets set nox/security/pin` is refused and points to
  `nox pin set`. An entry that is not a PIN hash (left by an older version) fails every check with
  the reason "PIN entry is not a Nox PIN hash - set it again with `nox pin set`", is reported by
  the `secrets` health check and `nox doctor`, and does not count as a wrong guess. The dashboard
  will not touch such an entry; `nox pin set` on the local machine replaces it.
- **Lockout**: 5 failed attempts trigger a 15 minute lockout, persisted in the database so a
  restart does not reset it, and audited. Verifications are serialised, so concurrent requests
  cannot spend several guesses on one count. A PIN change made with `nox pin` is not in the audit
  log: only the core writes its hash chain.

## Plugin sandboxing boundary

A plugin is a manifest (`plugins/<id>/manifest.yaml`) plus a worker process; nothing is spawned
before its manifest validates. A plugin can only: register tools inside its own `<id>.` namespace
(never a hard-prohibited name), emit/listen to the exact event names its manifest lists, read the
exact `nox/<id>/...` secret names it declared (the core resolves them; the value never persists in
the plugin process beyond the call), and reach only the `host:port` entries in its own
`network.egress` list — authorized against the *active profile's* allow-lists by the core before
the worker is ever spawned, then enforced a second time inside the worker by its own scoped
`EgressGuard`. Nothing a plugin does can widen what the core already validated. See
`PLUGIN_AUTHORING.md` for the manifest schema and full detail.

No process Nox starts outlives the process that started it. On Windows each child is placed in a
job object with kill-on-close. On macOS and Linux each child watches the exact parent it was given
(`NOX_PARENT_PID`, checked by pid and start time) and ends itself when that parent is gone, so a
crashed supervisor or core cannot leave a plugin or the voice worker running unsupervised.

## IPC authentication

"Loopback" is decided exactly (`nox.core.netloc.is_loopback`): an IP literal counts when
`ipaddress` says it is a loopback address (127.0.0.0/8, `::1`, their IPv4-mapped forms), a name
only when it is `localhost`. A name that merely looks local - `127.evil.example`,
`127.0.0.1.nip.io`, `localhost.evil` - is an ordinary remote host for the egress guard and the
plugin manifest check.

Both local servers refuse a request whose `Host` header does not name this machine on the port
they listen on (a DNS-rebinding page always sends its own name), and a browser `Origin` other than
the core's own pages (`http://127.0.0.1:<http port>`, `http://localhost:<http port>`, or the
configured `ipc.host`). A client that sends no `Origin` - the shell, workers, plugins - is not a
browser and passes on to the token check; `Origin: null` is a browser's opaque origin and is
refused.

The local WebSocket hub is loopback-only, versioned, and requires a per-client session token
(issued by the core at startup, stored under `%APPDATA%\Nox\runtime`) — every connecting client is
assigned a role (`supervisor`, `shell`, `dashboard`, `pet`, `worker`, `plugin`, `remote`), and
requests are checked against a per-role allow-list plus rate limits, not just "authenticated =
trusted". Security-sensitive requests (e.g. kill-switch resume) further restrict which roles may
even attempt them, regardless of token validity — see "Kill switch" above for a concrete example.

Workers and plugins do not use the session token. Each spawn gets a one-time token through its
environment, bound to the one client id it was spawned as and consumed by the first authentication
attempt. A successful first authentication returns a **reconnect credential** in the `ipc.auth`
response — never in an environment variable or on a command line — so only the process that won
the spawn token holds it. It is bound to that client id and to the spawned process (pid and start
time), it lets the worker authenticate again after a lost connection, and it is revoked as soon as
the core terminates the worker, sees it exit, or spawns a replacement; a restarted core never
honours an earlier core's credential. A worker the core refuses, or that cannot reconnect within
its deadline (30 s for a plugin, 60 s for the voice worker), shuts down and exits rather than keep
its device or network connections open unsupervised.

**One instance.** The supervisor and the core each take an exclusive OS lock
(`<runtime_dir>/supervisor.lock`, `<runtime_dir>/core.lock`) before they touch anything. A second
instance exits with "Nox is already running" and changes nothing: it never overwrites or deletes
the running instance's token files, port file or audit chain. The lock is released by the kernel
when the process ends, so a crash never leaves a stale lock.

## Security test obligations

Every control above has negative tests, not just positive/happy-path coverage: a denied tool, a
hard-prohibition bypass attempt via config removal, PRIVATE mode with a cloud provider selected, a
capture attempt while a zone is active, a kill switch triggered during streaming TTS, an audit
chain tamper, a secret appearing in a log line, an unauthenticated IPC client, and a
wrong-role request. These are tracked as a standing obligation (Security Model §11) and are part
of what `EXTERNAL_SECURITY_REVIEW.md` asks a reviewer to confirm is green before relying on this
document.

## Settings, secrets and the Twitch login

Settings are writable through `nox.settings`, and the write path is deliberately narrower than
the read path:

- **Configuration** — `config.set` accepts only an explicit allow-list of dotted paths
  (`nox.settings.schema.EDITABLE_PATHS`). Hard prohibitions, filesystem roots, profile rules,
  supervisor command lines and the IPC/token settings are not on it, so nothing reachable from the
  UI can widen what the security core enforces. Every value is validated by the configuration
  models themselves before anything is written, the write goes to the User layer (`user.yaml`)
  only — never to `config/defaults.yaml` — and the change is audited by path. Values are never
  audited, logged or put into the `settings.changed` event: a setting can hold a display name, a
  channel or a hotkey, and none of those belong in an append-only log.
- **Secrets** — `secrets.set` / `secrets.delete` accept only the six known names
  (`nox/twitch/*`, `nox/obs/websocket_password`, `nox/telegram/bot_token`). `nox/security/pin`
  is not among them; the PIN keeps its own path. There is no `secrets.get`: the UI can ask whether
  a secret is present, never what it is. Writes are rate-limited (10/minute) and, whenever a PIN is
  configured, require that PIN — verified the same way resuming from a security-path kill is.
- **Twitch login** — the OAuth 2.0 Device Code Grant (`nox.settings.twitch_auth`) replaces pasting
  a token by hand. It is a public client: no client secret is stored, the requested scopes are
  exactly `chat:read chat:edit`, and the access and refresh tokens go straight into the keyring.
  Every request goes through `core.security.egress.client()`, so `id.twitch.tv:443` has to be on
  the active profile's allow-list and the flow is impossible in privacy mode PRIVATE or OFFLINE.
  The refresh runs in the core, not in the Twitch plugin worker: a plugin may read its declared
  secrets but never write one, and that boundary is not relaxed for convenience.
- **Personality** — the character text lives in `<data_dir>/personality.md`, editable by the user
  (`personality.set`, or any text editor). The operating rules in the system prompt stay in code:
  the untrusted-data rule, "you cannot execute anything yourself" and "never reveal secrets" are
  security controls, and a security control that a text field can delete is not one.

All ten requests are registered for the `shell` and `dashboard` roles only — never `plugin`,
`worker`, `pet`, `supervisor` or the `remote` role a paired phone gets.
