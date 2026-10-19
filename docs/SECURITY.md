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
| Secrets in code/config/logs/prompts | Keyring-only storage; secrets are never serialized; a log/PII filter; the prompt builder excludes the secret store entirely. |
| Stream accidents (wrong scene, leaking screen, stream stopped by mistake) | Hard prohibitions (`stream.stop`, `stream.key.read`) - also for the generic `obs.request`, whose every call the core classifies by request type (see "OBS control" below); scene switches require confirmation in the stream profile; privacy zones hide windows from capture. |
| Anti-cheat / game integrity | No input-synthesis, no process injection, no memory reads exist anywhere in the codebase; hard prohibitions; sensors are observation-only. |
| A runaway or hung AI | Kill switch lives below the AI layer, in the supervisor; panic mode; budget limits. |
| Tampering with audit or security config | Hash-chain verified at boot; changes to the security core require a PIN; config changes are themselves audited. |
| Data loss | Checkpoints; the vault is the source of truth; Nox-written notes can be rolled back. |

**Out of scope**: an attacker with local admin on the machine, physical access, or a compromised
OS. Nox defends against a malicious/hallucinating AI layer and a malicious remote party — not
against someone who already owns your Windows account.


## OBS control

`obs.request` sends one obs-websocket v5 request of any type. Its risk is decided per call by the
core, never by the plugin (`nox.stream.obs_requests`, wired through `nox.plugins.scopes`):

- **Read** - listed read-only types without secrets run without asking.
- **Medium** - listed, easily undone changes (creating scenes, moving and sizing sources, muting,
  transitions, the local replay buffer) and reads that may return secrets (profile parameters,
  persistent data) are confirmed; "remember for this session" covers that one request type only.
  Each type has an allow-list of request fields; anything else in the request makes it high.
- **High** - asked every time, never remembered (`NEVER_REMEMBERED`, enforced by the engine, the
  shell's cache and the dialog): deleting or renaming, going live (any route), recording, the
  virtual camera, showing/hiding sources, turning filters off, unmuting, volume, audio tracks and
  monitoring, studio-mode transitions, switches outside the plugin's `scene_set`, sources or
  settings beyond the harmless allow-list (text, colour, size), replaced settings
  (`overlay: false`), UUID-addressed changes, and every request type not listed.
- **Never** - stopping the stream and reading the stream key hit the hard prohibitions; stream and
  output settings, profile parameters, hotkeys, vendor requests, file-writing screenshots and
  the record directory are classified critical; the privacy scene and everything in it (nested
  scenes and groups, resolved from OBS before each change, failing closed) cannot be changed.
  After a panic the plugin changes nothing.

Answers are redacted before the model sees them: one linear pass per string (capped at 4 KiB,
cut at a token boundary) reduces URLs to `scheme://host` (or redacts them), paths to their file
name and generated-looking runs to `[redacted]`; settings blocks keep only allow-listed keys.

What remains possible after the user confirmed a medium request type for the session: further
requests of that same type without asking - moving or cropping sources (which can uncover what a
cover image hid), changing a text source's words, switching among the listed scenes.

## Stream overlay credential

The pet page in OBS authenticates with a persistent overlay credential (IPC role `overlay`),
stored only as a SHA-256 in the data folder and revocable from the dashboard (revoking closes live
connections). The role may subscribe and call `state.get`, nothing else; it receives an
allow-listed, text-free view of the pet's events (`nox.ipc.pet_view`), nothing about
headphone-only speech, and nothing but the privacy mode while privacy is PRIVATE or OFFLINE.

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
  this path does not depend on the core process), the tray, the dashboard, the pet's own menu, and
  the spoken phrase "Nox, Notaus" (matched locally by the STT worker and forwarded as a
  `security.kill` request — it never goes through the LLM).
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
- **Panic mode** = kill switch + privacy forced to OFFLINE + pet hidden + stream-safe behavior
  (an OBS privacy-scene request in v0.2+). Panic never deletes anything.

## Rocket League observation-only boundary

Immutable, code-level, not a setting: screen/HUD capture, game audio, replay files, and
non-invasive input *observation* (hotkey-listener statistics, never sending input) are the only
things Nox's Rocket League feature does.

No file anywhere in the repository may import a process-memory API (`ReadProcessMemory`,
`WriteProcessMemory`) or a third-party input-automation library (`pydirectinput`, `pyautogui`).
`SendInput` is permitted in exactly one file, `src/nox/desktop/keyboard.py`, which exists so Nox can
type into an ordinary window - and which refuses to send anything at all while a watched game
process is running anywhere on the machine, not merely when the game window is the target. It types
characters (`KEYEVENTF_UNICODE`), which cannot express a key combination. There is no mouse input.

`game.input.send` remains in the immutable hard-prohibition list, which no profile, configuration,
plugin or model can lift.

All of this is enforced by CI rather than by convention. The `guard-security-model` job in
`.github/workflows/ci.yml` runs two checks on every push and pull request: the process-memory and
automation patterns must appear nowhere under `src/` or `plugins/rl/`, and `SendInput` must appear in
that one file and nowhere else - *and* that file must still contain its game guard. The second half
matters as much as the first: an exemption nobody re-checks is not an exemption, it is a hole.

## Secrets handling

`SecretStore`, backed by the `keyring` package (Windows Credential Manager on the platform Nox
ships for), names shaped `nox/<component>/<key>` (e.g. `nox/twitch/oauth_token`,
`nox/obs/websocket_password`). Set via `nox secrets set <name>` (value read from a hidden prompt,
never a command-line argument) or the onboarding wizard's equivalent prompts; a dashboard UI is
planned. Values are never logged, never included in a `repr()`, never audited, and never placed in
a prompt sent to an AI provider — the prompt builder excludes the secret store entirely. Plugins
only ever see the exact `nox/<their-id>/...` names their own manifest declares (see
`PLUGIN_AUTHORING.md`); the core reads the keyring on their behalf, so a plugin process never holds
a persistent handle to the store itself. Automated tests use an in-memory backend and never touch
the real Windows Credential Manager or a real credential.

## Untrusted content in a conversation (taint)

Every tool result Nox did not compose itself (a web page, a file, a window title, an MCP result,
the AI provider's own web search) taints the conversation it entered (`nox.security.taint`). Voice,
each dashboard client and each paired phone are conversations of their own, with their own history,
model context and taint. While a conversation is tainted, a remembered grant above LOW asks again,
and any call that sends data off the machine asks - so injected text cannot exfiltrate silently.
To keep this from turning into confirmation fatigue, a read-only lookup whose free text is
demonstrably taken from what the user said runs without asking. Two conditions, both required.
Positive: a `web.search` query (to the user's own search backend) is plain words forming an
in-order, repetition-free subsequence of one user message, at most 12 words; a `web.fetch` URL is
one the user typed (scheme only upgraded http to https, host and path byte for byte after the
fetcher's own parsing; percent-escapes, non-ASCII, userinfo and fragments never match); a curated
read-only shell command's arguments are the user's words or bare flags. Negative: the text copies
no 20-character run out of untrusted content that the user did not also write, and nothing
unreadable (the provider's own web search) entered the conversation. Only the user's messages
count, never memory, history or tool output. Everything else confirms. The taint is cleared only when the conversation's
context is really discarded: the user says "neues Gespräch" / "new conversation", or the
conversation sat idle for an hour (`conversation_idle_s`) - in both cases the next turn has no
history and a new model session. Nox has no context compaction; the warm session keeps everything
until then.

## Audit logging

Append-only, hash-chained (`prev_hash`/`hash`, SHA-256 over the canonical JSON of each entry plus
the previous hash) — `verify_chain()` runs at boot and on demand from the dashboard, so tampering
is detectable, not just discouraged. Logged: every security decision, mode/privacy change,
kill/panic event, config change, tool execution with side effects, memory deletion, and plugin
lifecycle event. The audit log itself is designed to never contain secrets or raw private content.

## PIN

Protects: hard-prohibition-adjacent settings, profile rule edits, an audit-log reset, and resuming
after a *security-path* kill (see "Kill switch" above — a user-initiated kill needs no PIN).
Stored as an Argon2id hash in the keyring (`nox/security/pin`); 5 failed attempts trigger a 15
minute lockout, itself audited.

## Command sandbox on Linux

Commands, self-built skills and foreign MCP servers run in `nox.sandbox`; on Linux and the
Raspberry Pi that is bubblewrap (`bwrap`):

- **Filesystem:** the system read-only, the work directory the only writable place, a private
  `/tmp`, `/dev` and `/proc`, no home directory and nothing of Nox's own folder.
- **Network:** every run gets its own network namespace with only its own loopback - no route
  to the internet, the LAN, the Home Assistant host or services on the host's loopback (abstract
  Unix sockets are per namespace, so the host's D-Bus/X11 are out of reach too).
- **Declared egress** adds exactly one path, and it is not a network path: the run's egress proxy
  listens on a Unix socket in a private `0700` directory, bwrap binds that one socket read-only
  into the sandbox, and a stdlib-only forwarder relays the sandbox's `127.0.0.1:3128` to it. The
  forwarder never parses or dials anything; the proxy outside checks the per-run credentials
  (passed in the environment, never on a command line), the declaration and `NetGuard`
  (loopback, private, link-local/metadata and the Home Assistant host are refused, even for `*`).
  Plain-HTTP clients must tunnel (`CONNECT`); the sandbox proxy does not forward `GET http://…`.
- **Ceilings:** memory and process limits through the service's delegated cgroup
  (`nox.service`: `Delegate=memory pids`), a `systemd-run` scope where one is allowed, otherwise
  none - `Sandbox.guarantees()` says which.
- **Fail closed:** bwrap and the egress path are probed when the sandbox is built. If bwrap
  cannot build its namespaces, nothing runs; if only the egress path fails, runs without declared
  egress still work and declared egress is refused.

For this, `nox.service` allows the namespaces bwrap creates and the `@mount` system calls inside
them, and does not over-mount `/proc` (`ProtectKernelTunables=`/`ProtectKernelLogs=` would make
the kernel refuse the sandbox its own `/proc`). The core itself still holds no capabilities.

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

## IPC authentication

The local WebSocket hub is loopback-only, versioned, and requires a per-client session token
(issued by the core at startup, stored under `%APPDATA%\Nox\runtime`) — every connecting client is
assigned a role (`supervisor`, `shell`, `dashboard`, `pet`, `worker`, `plugin`, `remote`, `mcp`), and
requests are checked against a per-role allow-list plus rate limits, not just "authenticated =
trusted". Security-sensitive requests (e.g. kill-switch resume) further restrict which roles may
even attempt them, regardless of token validity — see "Kill switch" above for a concrete example.

## Native tool calling and the MCP bridge

Language models call Nox's tools natively, and every call still goes through the same pipeline as
a click in the dashboard: `ToolLoop` -> `ToolExecutor` -> `PermissionEngine` -> audit. What the
model is allowed to reach is decided in code, below it:

- **Per-turn offer, token and budget.** Each chat turn opens a turn with the tools usable right
  now (profile, mode, privacy), a random token, and a `TurnBudget` (20 calls, 180 s, no fourth try
  of an identical failing call). A call outside the offer, after the turn ended, or past the
  budget is refused before the executor sees it, and the current offer is checked again on every
  call: a tool the profile, mode or privacy took away during the turn is refused.
- **Privacy mid-turn.** The gate asks before every call whether cloud models are still allowed;
  a switch to private or offline ends all open turns and interrupts Claude Code's answer. Turns
  said while cloud models were not allowed are stored marked private (sticky for the whole turn,
  question and answer alike) and never sent to a cloud provider later, not even as history;
  turns in privacy mode private are not stored at all.
- **Claude Code** runs as one long-lived process per core with its own tools switched off
  (`--tools ""`) and Nox's tools served over MCP by `python -m nox.mcp.bridge`
  (`--mcp-config <file> --strict-mcp-config --allowedTools mcp__nox__*`; the pre-approval only
  stops the CLI from asking itself - Nox decides). It ignores user and project settings, hooks,
  plugins, skills and auto-memory (`--setting-sources ""`, `--disable-slash-commands`,
  `autoMemoryEnabled: false` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY`), runs in a working folder of
  its own under the system temp folder with a home folder of its own, and gets an allow-listed
  environment: no Nox token or secret and nothing of an outer Claude Code session; only
  `CLAUDE_CONFIG_DIR` points at the user's Claude Code configuration, for the login.
  `tests/integration/test_claude_isolation.py` proves it with canaries (CLAUDE.md files around the
  working folder, hooks, the home folder, auto-memory). Anthropic's web search is allowed only in
  privacy mode full/balanced and at most five times per turn. Tool results reach the model as
  marked untrusted data.
- **Cost.** `ai.providers.claude_code.max_budget_usd` is enforced per answer while it runs, from
  the usage Claude Code streams (a conservative estimate at list prices); the answer is stopped
  with a sentence saying so. The CLI's own `--max-budget-usd` counts a whole session and is only
  the outer cap.
- **IPC role `mcp`.** The bridge authenticates once with a one-time token bound to the role and to
  its client id (a worker token cannot open the role, nor its token a worker role); the MCP config
  file that carries it is owner-only and deleted as soon as the bridge is connected. The role can
  call exactly `mcp.tools.list` and `mcp.tools.call`, receives no events and may not subscribe.
  Every call must carry the token of the open turn, which the core hands the bridge right before
  the turn, which works only for that bridge, and which dies with the turn. A bridge that drops
  out unexpectedly gets the session replaced before the next turn.
- **Kill switch.** It kills the Claude Code process tree - including the bridge, which Windows
  would otherwise leave running (a job object per session, psutil as fallback, never waiting on
  the event loop) - within a second, also while a session is still starting or the previous one
  is closing, ends every open turn (running handlers are cancelled), and nothing restarts until
  resume. After three failed starts or control requests the warm session is given up for the run:
  turns go to one-shot Claude without tools, and the provider card says so.
- **Local models.** Ollama gets the offer in its native `tools` field when the model declares
  support, otherwise as the text protocol (`NOX_TOOL_CALL`), parsed inside the provider so the
  line is never shown or spoken. Both end in the same tool loop.

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

## Known limits of network access

Paired devices in the home network (`nox.lan`, see [`NETWORK_ACCESS.md`](NETWORK_ACCESS.md)) are
protected as described there. Three limits are known and accepted for now:

- **The global pairing rate limit can be used up.** Wrong pairing codes are limited per source
  address and in total. Someone in the home network who floods the pairing endpoint uses up the
  total budget, and pairing then waits about a minute for everyone - including a legitimate new
  device. Already paired devices are not affected; nothing is unlocked by it.
- **First contact is trusted.** A device sees Nox's certificate for the first time while pairing.
  Someone who controls the network at that moment could put themselves in between, and the browser
  warning looks the same either way; comparing the SHA-256 fingerprint shown next to the QR code
  is the only check. Pair only on your own Wi-Fi, never on a network you do not control, and
  install Nox's certificate authority (its fingerprint is shown in the Devices tab) to make later
  connections verifiable.
- **Paired devices can use up the PIN's attempts.** A device's wrong PINs count towards the PIN's
  own lockout (5 wrong, then 15 minutes). One device is removed after its third wrong PIN, before
  it alone can trigger the lockout - but two or more paired devices in the wrong hands together
  can, and the PIN is then locked for 15 minutes on the computer as well. Nothing is unlocked by
  it; removing a device needs no PIN on the computer Nox runs on (or `nox unpair`), so the owner
  can always cut them off.
