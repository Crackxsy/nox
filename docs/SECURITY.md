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
| Stream accidents (wrong scene, leaking screen, stream stopped by mistake) | Hard prohibitions (`stream.stop`, `stream.key.read`); scene switches require confirmation in the stream profile; privacy zones hide windows from capture. |
| Anti-cheat / game integrity | No input-synthesis, no process injection, no memory reads exist anywhere in the codebase; hard prohibitions; sensors are observation-only. |
| A runaway or hung AI | Kill switch lives below the AI layer, in the supervisor; panic mode; budget limits. |
| Tampering with audit or security config | Hash-chain verified at boot; changes to the security core require a PIN; config changes are themselves audited. |
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
things Nox's Rocket League feature does. No file anywhere in the repository may import an
input-synthesis API (`SendInput`, `pydirectinput`, `pyautogui`) or a process-memory API
(`ReadProcessMemory`, `WriteProcessMemory`) for a game window. This is enforced by CI, not just by
convention: the `guard-security-model` job in `.github/workflows/ci.yml` greps all of `src/` for
these patterns on every push and pull request and fails the build the moment one appears, no
matter which module introduced it.

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
assigned a role (`supervisor`, `shell`, `dashboard`, `pet`, `worker`, `plugin`, `remote`), and
requests are checked against a per-role allow-list plus rate limits, not just "authenticated =
trusted". Security-sensitive requests (e.g. kill-switch resume) further restrict which roles may
even attempt them, regardless of token validity — see "Kill switch" above for a concrete example.

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
