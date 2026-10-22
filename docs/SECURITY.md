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
| A device in the home network reaches the node port (47844) | Mutual TLS 1.3: only a client certificate of this installation's authority completes the handshake; serial denylist, revoked pairings and the paired key are checked before the upgrade; no browser `Origin`. See "Nodes" below. |
| A compromised node (malware on a laptop, a stolen laptop) | Role `node`: a fixed JSON-RPC allow-list - no chat, memory, audit, config, prompts or other nodes; refused messages are audited and cut the connection; revocation closes it at once; certificates live 30 days. |

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

"Remember for this session" never applies to high risk, with one narrow exception: a
`shell.run` command that the strict allowlist classified SIMPLE (`nox.hands.shell.SIMPLE_ALLOWLIST`:
exact program, subcommand and flag spellings). Its target is
`cmd:<resolved executable>#<hash of its bytes> argv=<hash of the argv> <program> <arguments>`. The
argv hash (`nox.hands.shell.argv_digest`) covers the program's canonical name (lower-cased, without
`.exe`/`.com`) and every argument exactly as given, so a remembered `du -sh docs` does not cover
`du -sh notes` or `du -h docs`. The confirmation shows the readable command; the grant stores only
the two hashes as its scope, with the command as its label in the audit entry. Such a confirmation
may become a grant for exactly that argv, for the session only. A `cmd:` grant in any other shape -
a hand-written `cmd:git push`, or the older `cmd:<path>#<hash> <subcommand>` that ignored the
remaining arguments - matches nothing. Complex commands, command lines, `git`, interpreters and any
command with declared network egress are asked every time; a permanent grant exists only through
the visible list in the dashboard.

When the session ends, every session grant (`session:` and `cmd:`) is revoked
(`DefaultPermissionEngine.end_session`, audited as `grant.session_end` with its reason): when a
conversation is discarded - the user asked for a new one, or it sat idle past
`conversation_idle_s` (one hour by default) - and when the core shuts down. Grants are not per
conversation, so ending any conversation ends them all. Remembered shell commands end even sooner
(`revoke_command_grants`, audited as `grant.revoke_cmd`): when the privacy mode changes, when the
kill switch fires, and whenever untrusted content enters a conversation (taint, see below). While a
conversation is tainted, any other remembered grant above LOW asks again.

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
`SendInput` is permitted in exactly two files, and each one refuses to send anything at all while
a watched game process is running anywhere on the machine, not merely when the game window is the
target:

- `src/nox/desktop/keyboard.py` types into an ordinary window. It types characters
  (`KEYEVENTF_UNICODE`), which cannot express a key combination.
- `src/nox/desktop/computer_use.py` (owner approval of this change: 2026-10-18) moves and clicks
  the mouse and presses key combinations. It is **off by default**
  (`desktop.computer_use.enabled: false`); turning it on is a security setting and needs the PIN
  when one is set. Use happens in **sessions**: `computer.session_start` (risk high) asks every
  time - a "remember" on it is revoked the moment it is used - and only a confirmed session grants
  `computer.pointer` / `computer.keys` (risk medium), for the session's length
  (`session_minutes`) and action budget (`max_actions_per_session`) and no longer. While it runs,
  a `desktop.computer_use` event drives a visible notice; every action is audited. The kill switch
  and the stop hotkey (`ctrl+alt+shift+k`, read from the key state before every single event) end
  it at once. It refuses - in the module itself, first, and again immediately before every event -
  while a watched game or an anti-cheat service is running and while a game is in the foreground.
  Before every single event (each button down and up, wheel turn, key down) it checks the window
  under the cursor (`WindowFromPoint(GetCursorPos())`) and the foreground window against Nox's
  own windows - by process, by Qt window class and by a title naming Nox (the dashboard tab, while
  it is the browser's active tab) - refuses to move onto one, and sends nothing while a Nox
  confirmation waits. That check races by nature, so it is not the boundary. **The boundary is in
  the core:** while a session is open and for three seconds after Nox's last event, an "allow"
  answer to a confirmation is refused (a "deny" always counts) and the dashboard and shell keep
  only reading and stopping requests - no skill activation, no MCP approval, no settings. The user
  ends the session with the stop hotkey, which this module never sends, and then answers. Every
  combination with the Windows key, Ctrl+Esc, Alt+Tab, Alt+Esc, Alt+Space, Ctrl+Alt+Del,
  Ctrl+Shift+Esc, the stop hotkey and Alt+F4 on Nox are refused as a convenience. Key-ups after an
  interrupted combination are not sent into a game that came to the foreground; the session ends
  instead.

`game.input.send` remains in the immutable hard-prohibition list, which no profile, configuration,
plugin or model can lift.

All of this is enforced by CI rather than by convention. The `guard-security-model` job in
`.github/workflows/ci.yml` runs `scripts/check_input_synthesis.py` on every push and pull request,
over `src/` and every plugin under `plugins/`: the process-memory and automation patterns must
appear nowhere; `SendInput`, `keybd_event`, `mouse_event` and `SetCursorPos` only in those two
files; `PostMessage`/`SendMessage` with keyboard or mouse-button messages nowhere - *and* each of
the two files must still contain its game guard (`GameRunningError` and `if game_running:`). The
second half matters as much as the first: an exemption nobody re-checks is not an exemption, it is
a hole. `tests/unit/security/test_input_synthesis_guard.py` runs the same script locally.

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
model context and taint. Every remembered shell command (`cmd:` grant) is revoked whenever
untrusted content enters any conversation. While a conversation is tainted, any other remembered
grant above LOW asks again, and any call that sends data off the machine asks - so injected text
cannot exfiltrate silently.
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
- **No nested namespaces:** with bwrap 0.8+ the sandbox's user namespace forbids creating
  further ones (`--disable-userns`); older bwrap is reported in `guarantees()`. On Ubuntu 23.10+
  the installer gives only Nox's own bwrap copy (executable by the `nox` user alone) an AppArmor
  profile that allows user namespaces - the system's `bwrap` stays restricted. What it starts is
  stacked under a second profile that denies every capability and new user namespaces, on top of
  bwrap's own capability drop and `--disable-userns` (the copy is refused if it is older than 0.8
  or differs from `/usr/bin/bwrap`, and when the sandbox is built Nox reads the AppArmor label of a
  process the copy started: unless it carries `nox-bwrap-sandboxed`, the stack did not apply and
  the sandbox fails closed).
- **Environment:** bwrap, systemd-run and the cgroup trampoline run with a fixed environment
  (`PATH`, `LANG`; plus the bus variables `systemd-run --user` needs). The child's own variables
  - a server's settings, the proxy credentials - reach bwrap through a pipe (`--clearenv`,
  `--args <fd>`), never its command line or its environment, and only names on the allowlist
  (`model.checked_env`) are accepted at all.
- **Fail closed:** bwrap and the egress path are probed when the sandbox is built. If bwrap
  cannot build its namespaces, nothing runs - no command, no skill test, no local MCP server
  (only the user's `mcp.allow_unsandboxed` starts those as plain processes); if only the egress path fails, runs without declared
  egress still work and declared egress is refused.

For this, `nox.service` allows the namespaces bwrap creates and the `@mount` system calls inside
them, and does not over-mount `/proc` (`ProtectKernelTunables=`/`ProtectKernelLogs=` would make
the kernel refuse the sandbox its own `/proc`). The core itself still holds no capabilities.
This is a deliberate trade-off: the unit cannot set `RestrictNamespaces=` or deny `@mount` for the
core, so code running *as the core* could create user namespaces and mount inside them (still
without capabilities in the initial namespace, and on Ubuntu only through Nox's own bwrap copy).
What the sandbox runs gets neither: bwrap's `--disable-userns`, its capability drop and, on
Ubuntu, the stacked AppArmor profile forbid both.

## Command sandbox on Windows

On Windows the same `nox.sandbox` runs each workload in an **AppContainer** (`nox.sandbox.windows`),
falling back to a low-integrity token only where no AppContainer profile can be created:

- **Containers:** shell commands run in `Nox.Sandbox`; every other workload class (each MCP server,
  skill and skill test) gets one container of a fixed pool, `Nox.Sandbox.w000` to
  `Nox.Sandbox.w127` (`nox.sandbox.containers`). Slots are assigned first come, first served, and
  recorded in `<nox home>/sandbox/container-slots.json`; a workload's name never picks its slot, a
  slot never passes to a second workload class, and a full pool refuses the run. The pool is fixed
  so the container SIDs - a pure function of the name - are known at install time. **Limit:** a
  slot is never freed, not even when its workload is removed (its profile folder could still hold
  what the first workload left, and a second class must never see it). One Nox home can therefore
  run at most 128 distinct non-shell workload classes over its lifetime; the 129th is refused with
  a message saying so. Deleting `container-slots.json` resets the table only together with
  `nox sandbox reset` and a fresh Nox home - it is not done automatically.
- **Filesystem and secrets:** no capabilities, so no user files, no credential store; the work
  folder is ACL'd to the user, SYSTEM and the run's own container SID only. Python runs get read
  access to Nox's own runtime folders, recorded and removed by `nox sandbox reset`.
- **Network without the rules:** no internet or LAN capability. Loopback isolation, which should
  keep a container off the host's loopback services, is **not enforced on every Windows build**
  (measured on Windows 11 build 26200 with the firewall on and no loopback exemption: a container
  reached any host loopback listener).
- **Network rules (installer, one UAC prompt) - an allowlist by construction:** the installer's task
  "Keep sandboxed commands away from this computer's local services and the home network" adds
  persistent **Windows Filtering Platform** filters - one provider, one sublayer with the highest
  weight, six filters per container SID (shell's and all 128 pool slots):
  1. at the outbound connect layer and the inbound accept layer, IPv4 and IPv6: a **hard block of
     everything** (weight 1, clears the action right);
  2. the one exception, weight 15: TCP to `127.0.0.1` on **that container's own block** of four
     forwarder ports at the connect layer (shell's container 47100-47103, pool slot *n*
     47104+4*n ... 47107+4*n; all blocks together are 47100-47615, below Windows' dynamic port
     range), and the forwarder accepting exactly such a connection at the accept layer. The blocks
     never overlap, so a workload cannot even connect to another workload's forwarder. Four ports
     per container because concurrent runs of one workload class share its container.

  There is no list of private ranges to keep complete: loopback, the LAN, this computer's own LAN
  and VPN addresses, IPv4-mapped `::ffff:0:0/96`, NAT64, 6to4 `2002::/16`, Teredo and the public
  internet are all covered by the block of everything. WFP rather than Defender Firewall rules: the
  filters live in Nox's own top sublayer and the blocks clear the action right, so no permit
  elsewhere - Windows' own loopback handling, a third-party firewall's rules - opens them; and they
  are enforced by the Base Filtering Engine even when a third-party firewall has replaced Defender
  Firewall. `nox sandbox firewall show` prints every filter.
- **No code from a user-writable folder ever runs elevated.** Nox installs per user into
  `%LOCALAPPDATA%\Programs\Nox`, which the user's account (and so any malware running as that
  user) can change - a `.pth` file, the standard-library zip or a DLL next to `python.exe` would
  run with administrator rights if that Python were started elevated. So the elevated step never
  starts Nox's Python. It starts the system's own
  `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe` (by full path, `-NoProfile`) with a
  short bootstrap on its command line that reads the helper script `nox-sandbox-rules.ps1` into
  memory, checks its **SHA-256 against the value pinned into the installer at build time**, and
  runs exactly the bytes it checked - a copy changed before the check is refused, one changed after
  it is never read again. **No value is ever embedded as PowerShell source** in that elevated
  command line: the helper's paths arrive as the hex digits of their UTF-16 code units and are
  decoded by the bootstrap, so no character of a path (an apostrophe, a typographic quote U+2018 to
  U+201F that PowerShell also reads as a quote, `$`, a backtick) can end a string literal. As a
  second line of defence, the installer (`SandboxPathSafe`) and `wfp_helper.path_is_safe` refuse a
  path with quotes, typographic quotes, `$`, backtick, `;`, `&` or control characters before
  anything starts; letters outside ASCII (a user name like "Jürgen") stay allowed. The action is
  one of three fixed words. The helper (`nox.sandbox.wfp_helper`) is self-contained: .NET only, no
  cmdlet (so no module autoload from a user-writable module path), no compiler and no temporary
  file (the WFP calls are P/Invoke methods emitted in memory, `fwpuclnt.dll` by its full system
  path), and no input but its action - it derives the container names, SIDs and filter keys
  itself, exactly as Nox does (a CI test runs it and compares the output line by line).
- **Protected copy and status:** the elevated install first stores the verified bytes in
  `%ProgramFiles%\Nox Sandbox Rules\nox-sandbox-rules.ps1`. The folder gets an explicit, protected
  ACL - Administrators and SYSTEM full control, Users read and execute, owner Administrators,
  inheritance from Program Files cut - and the helper reads the ACL of the folder and the file back
  and refuses if anyone else could write, own or re-ACL them (or if the folder is a reparse
  point). The result goes to `HKLM\SOFTWARE\Nox\SandboxRules` (admin-write, user-read), never to a
  file in the user's temp folder. Uninstall runs **that protected copy** when its SHA-256 still
  matches the pinned one (else the equally checked copy in the install folder), removes every
  filter of Nox's provider - whoever added it, an older build or its command line included - the
  sublayer, the provider, the status key and the protected folder. It does so whenever the rules
  may be there (the folder, the status key, or Nox's provider found by a non-elevated check),
  independent of the install task. The installer writes a fresh uninstall log
  (`UninstallLogMode=overwrite`), so an older build's elevated `python.exe` uninstall step never
  runs again.
- **The command line refuses:** `nox sandbox firewall install|remove` (and `python -m
  nox.sandbox.wfp install|remove`) never change the rules - elevated or not, they refuse and print
  the protected helper's command for an administrator prompt - by absolute paths from the Windows
  API, never `%SystemRoot%`/`%ProgramFiles%`, which the user's own environment can redirect;
  `show` works for anyone.
- **The probe decides:** the first run measures whether a container reaches a host loopback listener
  (`host_loopback_reachable`). Only an explicit "refused" report counts as confined, and only while
  nothing on the host listens in the forwarder's port range; then `confines_network` is true, the
  AppContainer counts as strong and shell commands run without `hands.accept_weak_sandbox`. A
  timeout, a crash, a connection or an unknown listener table counts as reachable (fail closed).
  The rules are never assumed: without them, or if they do not take effect, the probe says so.
- **Declared egress:** the run's proxy listens on a named pipe whose DACL names only the run's
  container SID; a forwarder in the same container listens on `127.0.0.1` in that container's port
  block and relays into the pipe. The proxy checks the per-run credentials (random per run, `407`
  for anything else - another run's credentials included), the declaration and `NetGuard`.
  **Before every run that uses the forwarder**, Nox checks the listener table: the forwarder's port
  must be held by the forwarder process Nox just started, identified by PID *and* creation time
  (a reused PID does not match; Nox also holds the process handle, so the PID cannot be reused
  while the run lives), and every other listener in the container's block must be one of Nox's
  own live forwarders. **While the run lives** the same check repeats every 1.5 seconds; a foreign
  listener that appears in the block, or a forwarder that lost its port, ends the run (a finished
  `run` is reported as refused, a long-running `start`ed process is killed). Otherwise - a foreign
  listener, an owner that cannot be read, a table that cannot be listed - the run is refused (fail
  closed) instead of being given a path to a listener that is not Nox's.
- **Why not one port and one permit per run:** the filters are added once, by the elevated
  installer step; Nox itself never runs as an administrator, so it cannot add or change a WFP
  filter per run. The per-container block is the narrowest scope fixed rules can give.

Known limits, accepted:

- A **host** program that starts listening inside a container's port block *during* a run could be
  reached from that container for at most one check interval (1.5 s) before the run is ended.
- Windows can **reserve** a container's port block at boot (an excluded port range, typically
  Hyper-V, WSL or Docker). The forwarder then finds no free port and the run is refused, with the
  reserved range and `netsh interface ipv4 show excludedportrange protocol=tcp` named in the error;
  the fixed rules cannot move the block elsewhere.
- The probe measures shell's container; the pool containers carry the same filters, built from the
  same code, but are not probed one by one.
- With the rules in place a workload cannot reach its **own** loopback outside the forwarder range
  (a skill test that starts a local server and connects to it fails).
- UAC is the boundary Nox relies on, and Microsoft does not treat UAC as a security boundary
  against malware already running as the same user: such malware can show its own UAC prompt, or
  inject into elevated .NET processes through per-user environment settings. Nox's step adds no
  new path - the code it runs elevated comes only from the pinned bytes - but it cannot close these
  generic ones.
- If the uninstall's UAC prompt is declined, the filters stay. They only ever match Nox's own
  container SIDs; run the protected helper's `uninstall` from an administrator prompt
  (`nox sandbox firewall remove` prints the command) to remove them.
- The real install/remove against the Base Filtering Engine is exercised only where CI runs as an
  administrator (the hosted Windows runners do, `NOX_WFP_LIVE=1`); a clean-VM pass of the whole
  installer (install with the rules, `nox sandbox firewall show`, the probe reports confined, a
  shell command and declared egress work, uninstall leaves no filter) is still to be done by hand.

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

## External MCP servers

Nox can use other people's MCP servers (`mcp.servers`, or "Faehigkeit hinzufuegen" in the
dashboard). Their tools appear as `mcp.<server>.<tool>`, always risk **medium** (the permission
engine asks; "remember" is a grant for that one tool) unless the *user* marked a tool read-only -
what a server claims about itself (`readOnlyHint`) is shown, never trusted. Before a server starts:
`npx`/`uvx`/`pipx run`/`pnpm dlx` commands must name the exact version the user pinned; a shell is
never the program; environment values and headers that look like credentials must be secret
references `secret:nox/mcp/<id>/<name>` resolved from the secret store at start; variables that
change what code an interpreter loads (`NODE_OPTIONS`, `PYTHONPATH`, `LD_PRELOAD`, ...) and Nox's
own are refused; remote servers need HTTPS (plain HTTP only to loopback), go through the egress
guard and never to the Home Assistant host. The first start, and every start after the command
changed, waits for the user's approval of that exact command (bound to a fingerprint). Names are
checked against the hard prohibitions in every spelling, the reserved namespaces (`security`,
`home`, `desktop`, `game`, `permission`, `mcp`) and the home boundary; descriptions and schemas
are cut, cleaned of control and direction-override characters and labelled as untrusted data;
results are labelled the same way. A call whose arguments name a lock, alarm, valve or garage door
is refused before the server sees it, and a server that uses the network is not called in the
privacy modes private and offline. The client offers servers nothing - no sampling, no roots, no
elicitation. Where the sandbox can be built, a local server runs inside it (`Sandbox.start`,
its own private work folder, no egress unless it declares network, then public hosts only through
its own proxy, with the same network guard as a shell command - the Home Assistant host and its
addresses refused, every egress decision audited). Where it cannot (no backend, `bwrap` missing
or not the pinned build, the user-namespace probe failing, any other error), local servers fail
closed: none is started and its status says why (`blocked`). Only `mcp.allow_unsandboxed: true`,
set by the user in `user.yaml` (not a dashboard setting, not reachable by any tool), runs them
without the sandbox - with the plugin workers' environment allow-list, inside the core's job
object, without a console window: process hygiene, not isolation; the dashboard says
`sandboxed: false`. Every tool of a remote server, and of a local server that declares network,
is a network tool for the permission engine: refused in private and offline, and asked again in a
conversation that holds untrusted content, even when the user marked it read-only. Removing or switching off a server and approving a changed command revoke
every "remember" for its tools (`session:mcp/<id>.`).

## Self-built skills

`skill.learn` (risk high) lets Nox build a missing capability as a plugin under `<nox home>`, never
in its source tree. The builder is a Claude Code session with `--tools Read,Edit,Write,Glob,Grep`
on one work folder - no shell. What it may read or write is pinned, not left to the CLI's
defaults: `dontAsk` mode with exactly `Read(./**)` and `Edit(./**)` allowed (its work folder; Read
binds Glob and Grep, Edit binds Write) and `--setting-sources ""`, so no settings file - not even
one it wrote into its own folder - can widen that; a canary next to the folder stays unread
(`tests/integration/test_skill_builder_isolation.py`). The confirmation shows the whole wish, and
the wish reaches the builder with its delimiter tags defused. The core, not the model, then checks the result: manifest, id and
tool names (reserved namespaces, hard prohibitions, home boundary, core namespaces), every tool at
least risk medium, events only in its own namespace, egress only to declared public hosts (never
loopback, LAN or the Home Assistant host, and every URL in the source must be declared), and a
static scan (`nox.skills.scan`) - an allow-list of imports, module attributes and names - as
defence in depth.

**The sandbox is the boundary.** A skill's tests and its worker run only inside `nox.sandbox`
(AppContainer / `bwrap`), never as a process with the user's rights: no hub token, no access to
`<nox home>` or the keyring, egress only to the hosts its manifest declares through the process's
own proxy. Its files reach the sandbox as a bundle on stdin; the worker speaks JSON lines and the
core relays only `plugin.register`, `plugin.secret.get` (declared secrets, per call), `state.get`
and `worker.heartbeat`, plus the events the manifest declares. Without a sandbox that hides files
and secrets (`Guarantees.strong`) nothing is learned or switched on (fail closed). **The test
verdict is advisory, not a security control:** the same model writes the skill and its tests, so a
hostile skill can always pass its own tests; the summary says so. Reviews that call the verdict
forgeable are right, and that is accepted here.

A passing version is frozen and bound by a hash; activation is the user's click in the dashboard -
never a tool - and needs the PIN (when one is set) only when the skill wants a host outside the
allow-list or a secret. An approved host belongs to that skill only; deactivating, forgetting or
replacing the skill drops it and every "remember" for its tools. `skill.*` is never rememberable.
Learning is offered only for a missing capability: forbidden wishes are refused and a disabled
capability gets its fix. Everything is audited; "Vergessen" deletes it with its versions.

## Watchers

A watcher (`watch.create`, risk medium, asks every time - never remembered) calls exactly one
tool on an interval of at least five minutes and reports through the proactive service. The tool
must declare no side effects and be risk read, or be one of the named read-only fetchers
(`web.fetch`, `web.search`); the confirmation shows the address a fetcher will call. This is
checked again before every run, so a tool re-registered with more power is never called. The
condition is data (`lt`/`gt`/`contains`/`changed` on a dotted path), never code; a threshold must
come from the user or from memory, never a guess.

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

## Signed releases

Updates are code that runs as root on a server, so they are checked against a signature, not just
a checksum ([`RELEASES.md`](RELEASES.md)). Each release carries `release-manifest.json` (version,
tag, source commit, every file with size and SHA-256) and an Ed25519 signature by the project's
release key. `scripts/install-linux.sh` and `nox update` - the latter with the *running* version's
`nox release verify`, before the new version's installer runs - refuse a release when the manifest
is missing, its signature is invalid or made by a key that is not trusted or has expired, it names
another tag or commit, or it is older than the newest release the machine already accepted
(rollback protection; a deliberate downgrade needs `--allow-downgrade`). Files are then accepted
only with the size and SHA-256 the signed manifest lists. The trusted public keys are built into
each release with an expiry date, which is how keys are rotated; the private key never enters the
repository and is only available to the release workflow's protected `release-signing`
environment. While no key is configured, every verifier says "not configured" and nothing claims
to be verified. Negative tests: `tests/unit/release/` and the "Signed releases" step of the Linux
installer CI job.

## Nodes (`nox node`)

A node is Nox on another PC in the home (spec wave C), paired like a phone but holding a **client
certificate** of this installation's authority instead of a token. What protects the core so far
(`nox.node`, `nox.lan.tls`):

- **Pairing pins the authority.** The pairing link (`nox-node://pair?h=..&p=..&c=..&fp=..`, shown
  on a trusted session - the dashboard on this machine, or a paired device with the PIN)
  carries the authority's SHA-256 fingerprint, so the new node refuses a look-alike before it
  sends the code. `POST /api/node/pair` refuses any request with an `Origin` header (no browser),
  shares the pairing rate limit, and costs no code for a malformed certificate request.
- **What the authority signs.** Only the request's P-256 key is used, with proof of possession;
  the certificate says `CN=node:<id>`, SAN `urn:nox:node:<id>`, EKU clientAuth only, not a CA,
  30 days. Whatever else the node asked for is ignored.
- **The node listener** (`ipc.lan.node_port`, 47844) is separate from the dashboard's: TLS 1.3
  only, client certificate required. After the handshake and before the WebSocket upgrade, the
  certificate must be a node certificate whose serial was issued to that node and is not on the
  denylist, whose pairing is not revoked, and whose key is the paired one. The standard library
  has no verify callback inside the handshake; nothing is read from the peer before this check.
  A TLS handshake gets 5 s, the HTTP upgrade and the `initialize` 5 s each; after the certificate
  check at most 2 connections per node wait for their hello (a newer one displaces the oldest, so
  one node - or one address behind a NAT - never locks out another) and 16 in total. Before the
  certificate is known only the handshake timeout and the listen backlog bound a connection -
  a flood from the home network can cost CPU, never access.
- **Role `node`** never reaches the hub (`HUBLESS_ROLES`, no events). On its listener a node may
  send only `nox.node.protocol.NODE_TO_CORE`; the first message must be `initialize`, models
  forbid unknown fields, a frame is at most 1 MiB and a chunked result 8 MiB. Five refused
  messages close the connection. A node's `security.kill` always engages the kill switch while
  Nox runs - stopping is always safe, so a compromised node can at worst stop Nox until
  "Fortsetzen" - and is never throttled then; while Nox is already stopped, repeats change
  nothing, are answered `already_engaged` and audited at most once per 10 s.
- **Revocation** (`node.unpair`) revokes the pairing and every certificate of the node, sends
  `nox/revoked` and closes the connection in under a second; a node revoked while offline is
  refused on its next handshake.

On the node itself (`nox node`, [`NODES.md`](NODES.md)) - a compromised core can send
well-formed calls, so the node decides on its own authority (`nox.node.gate.NodeGate`):

- **Pairing pins the authority** before the code leaves the device: the authority is fetched,
  hashed and compared with the link's fingerprint; a mismatch sends nothing. The answer must be a
  certificate for the node's own key from the pinned authority. Later connections trust only the
  pinned authority (TLS 1.3); a stored authority that no longer matches the pin stops the node.
- **The key never leaves the device**; it is encrypted with a passphrase in the OS keyring where
  there is one. Residual risk: on a headless Linux node only its 0600 mode protects it.
- **The gate, before any handler:** hard prohibitions (the core's immutable list, by name and
  glob), known tools only, the tool's capability group enabled in the device's `node.yaml` and not
  narrowed by the core, the risk ceiling `max_risk` (default high, critical never) and the core
  may not claim a lower risk than the node's own; a **signed core receipt** for every call with
  a side effect, checked *before* any argument is looked at: an Ed25519 signature by the core's
  receipt key (pinned at pairing) over call id, this node and this core, tool, the hash of the
  NFC-normalised arguments, risk, who decided on the core (`user`, `grant`, `profile`,
  `device`), delegation, taint, issue time and deadline (at most 60 s after issue, on the core's
  clock as measured from its pings); missing, altered, for another node, from the future,
  expired or replayed -> refused. The arguments are normalised once; the same text is hashed,
  shown and run. Then strict argument models and a **text-only** check: no argument may name the
  node's own folder, and any path alias that only the file system could resolve (`..`, `~`,
  8.3 short names, UNC and device paths) is refused - the gate never resolves, expands or opens
  anything the core sent (a UNC path would otherwise make Windows reach out over SMB). The real
  path is checked by a file tool at the moment it opens it; every node tool with a side effect
  must declare that (`path_safe`) or it is not offered. Then the node's **own budget** (60 side
  effects and 5 high-risk calls per hour by default, charged at the higher of the node's and the
  core's risk, read back from the node's audit after a restart). The core can only narrow the
  groups (`nox/policy`).
- **High risk is confirmed on the device itself** (owner decision D1): whenever the node's tool,
  the core's receipt or the core's delegation says high, the node asks in a system dialog on that
  device - every argument in full (control characters escaped, no markup; too long or not
  valid Unicode -> refused, never cut), a warning when the conversation held foreign content,
  60 s, default **no**; a broken dialog is a no, and a "yes" within 1.5 s (a reflex or a stray
  key) is a no. While it is open, every other running call with a side effect is stopped and
  new ones are refused (only reads go on). Risk levels are compared by rank, never by spelling:
  "critical" from the core counts at least as high, and an unknown value as the highest. Linux uses zenity only (`--default-cancel`, `--no-markup`). The core leaves the
  question to the device only when the node said at `initialize` that it can ask; otherwise the
  core asks itself, and a node without a screen runs high risk only when a **person** confirmed
  it on the core (`decided_by: user`) and `trust_core_for_high` was confirmed on that device
  (`nox node allow trust_core_for_high`; documented residual risk). The answer comes back as
  `nox/confirm_result` and is audited on both sides. A real presence check on the device
  (Windows Hello, polkit) is a later task.
- **Stopping:** the core's kill switch sends `nox/kill` to every node over the open sockets; a
  node cancels every running call at once, answers `nox/killed` and refuses everything until
  `nox/resume` - under a second to five nodes on loopback in the test suite (the real-hardware
  p99 is on the release checklist). Dead man: no word from the core for
  3 s while a side effect runs -> that call is aborted and the node refuses everything until the
  core has pinged it three times in a row while not stopped; every `nox/pong` tells the core why
  a node refuses (`state`). `nox node stop` on the device stops it there and the core
  everywhere; only the device lifts it (`nox node resume`). A session ends when its certificate
  does.
- **Widening needs a person at the device:** `nox node allow` only in an interactive terminal
  and after a typed "yes" - a boundary against the core and remote tools, **not** against a local
  process of the same user (a real presence check, Windows Hello / UAC consent or polkit, is a
  later task). The confirmed manifest is recorded in the node's audit trail with an HMAC under a
  key made at pairing (OS keyring, else `manifest.key` 0600 in the node folder); the MAC covers
  the entry's place in the chain and a counter kept with the key, so an old confirmation appended
  again is rejected. Pairing confirms only the defaults, never a `node.yaml` that was there.
  The node applies `node.yaml` only as far as a valid confirmation reaches; an edit of the file
  widens nothing, narrowing applies at once (also to a running node). A missing key, a broken
  chain or a forged confirmation leave the node with system values only until it is confirmed
  again locally.
- **The dashboard only narrows** (`node.narrow`, `nox.node.policy`): switching a group off needs
  no PIN on the core machine (from a paired device the PIN); the core then routes nothing of that
  group - by its own tool-to-group mapping, never the node's word, and a tool of no known group
  is dropped - and sends `nox/policy` on every connection. Clearing that narrowing needs the PIN
  when one is set (always from a paired device) and can never reach past what the device itself
  enabled, which the node reports with every tool list; anything else is refused as
  `enable_on_device`. `node.defaults.set` (where a tool family runs when nobody names a device)
  is administration like `node.link`; removing a node resets the defaults that named it. Every
  change is audited with its outcome, every refusal after authorization; a change once accepted
  runs to its end even when the asking dashboard disconnects, so the database, the routing and
  the node never disagree.
- **Captures, browser, desktop, computer use and media on nodes (C6).** All off by default
  except `media` (a node announces it once on the device). `screen.capture` and `camera.snapshot`
  are high risk on a node (the device's dialog every time, never remembered), need this core's
  privacy to allow that kind of capture - checked before the node is asked **and again after the
  device's answer** (panic, safe mode or a stricter mode meanwhile discards the picture unseen) -
  and are refused on the node unless a visible notification could be shown first
  (`no_visible_indicator`). The PNG is held in memory for the call only, made smaller on the node
  when it is over 8 MiB (longest side 2560 px, still PNG; refused if that is not possible), sent
  as `nox/blob` chunks, checked by SHA-256 and PNG signature on the core and stored in the core's
  captures folder, so `vision.describe` keeps its rules (camera pictures never to the cloud).
  `browser.*` runs Nox's own headless Chromium behind `NetGuard` on the node (no LAN, no
  loopback); `browser.screenshot` is not offered there. `desktop.*` and `computer.*` are Windows
  only and reuse the core's modules unchanged (boundary, self-approval lock, stop hotkey,
  per-event game guard); window titles from a node are hidden while this core is in panic or
  safe mode. A routed call may wait for the device's dialog: the node call's timeout grows by the
  dialog's minute for high risk, and the executor waits for it.
- **Game lock on nodes (fail closed).** `NodeGate` refuses mouse, keys and typing
  (`computer.session_start/pointer/keys`, `desktop.type_text`) and every change to windows and
  programs (`desktop.window_*`, `desktop.process_stop`) - before the stop state, the capability
  or the receipt are even looked at - while any of these holds, each read fresh on every call
  (`nox.node.world.GameLock`, `nox.desktop.anticheat`): a watched game runs (the core's default
  list, the device's own `node.yaml` `games` - `nox node games add` adds at once, removing is
  asked in a terminal and recorded like a widening, an edit of the file can only add - and the
  core's list, pushed as `nox/games`, an addition only); an anti-cheat process (also by family
  name), service or kernel driver runs (Vanguard, EasyAntiCheat, BattlEye, FACEIT, ACE,
  nProtect, PunkBuster, XIGNCODE, mhyprot, Ricochet and more, by `QueryServiceStatus`); a game or
  anti-cheat process is in front; something owns the screen (`SHQueryUserNotificationState`:
  full-screen Direct3D, busy, presentation mode - games on no list are caught here; a Store app
  in front, `QUNS_APP`, does not lock, a game among them is caught by name or exclusive full
  screen); or any of
  these checks cannot answer, including an unreadable foreground. Without a lock installed the
  gate refuses as well. Listing windows and processes stays allowed. `game.input.send` stays a
  hard prohibition. Enabling `computer` on a device asks twice and is refused off Windows.
- **Self-approval lock on nodes (fail closed, the node is the authority).** The node reports its
  truth about computer use - `session: {gen, active, id}`, where `gen` is a random epoch per run
  plus a counter that grows with every session start and end - in its hello, every pong (each
  second) and every tool answer, refusals included. The core keeps per node the highest
  generation seen and derives the lock from it monotonically: an older generation is ignored,
  the same one updates `active`, an unreadable report changes nothing; only an authenticated new
  hello (a new run, a new connection) starts over from its report. The lock holds while the node
  says a session is open (unreachable: until it says otherwise or 120 minutes have passed since
  it last said "open") or while a start is on its way - a start is settled only by its own
  answer's report or a newer generation; a cancelled start ends the session on the node in the
  background. The lock never lifts while the node keeps saying "open" - only its report that no
  session runs, or its silence for 120 minutes, lifts it; a session's own length is limited on
  the node (`ComputerUseConfig.session_minutes`), and a new session back to back gets a fresh
  grant. While it holds, **every "allow" and every request that changes something** (adding
  an MCP server, approving a plan, any setting - everything but reading, "deny" and Stopp, which
  always work) goes through one rule (`session_refusal`, in `security.permission.reply` and in
  `RequestRegistry.dispatch`), because Nox does not know the PIN and the PIN is the guard: (1)
  the browser device linked to a node Nox controls never approves or changes anything; (2) any
  other paired device and this machine's own dashboard or shell only with the security PIN, sent
  with the request (`pin`; the PIN's lockout applies, and a paired device is removed after
  repeated wrong PINs); (3) this machine's dashboard or shell never from a remote session -
  Windows: `SM_REMOTESESSION` or a session that is not the console's, or no console session at
  all; Linux: an SSH session or `loginctl` `Remote=yes` - and refused when that cannot be told; a
  core that runs as a service without any login session (the Pi image) counts as not remote, the
  PIN applies; the check runs off the event loop with a time limit (too slow: unknown); (4) no PIN
  set: refused. The desktop shell's dialog has a PIN field for this. A node's own dialog still
  asks on that node. **Talking to Nox stays possible** (`chat.send`, `voice.ptt`): no PIN from
  this machine (but still not from a remote session) or from a paired device other than the
  controlled PC's browser, which is refused (Nox could type its own instructions there). The
  controlled PC's browser is every paired device linked to that node **and every paired device
  connecting from that node's address** (its address as this core sees it, and the addresses the
  node reports in its hello). A paired device's message during the session enters the
  conversation as **untrusted content**, like what Nox read from the screen in that session, so
  it widens nothing the taint rules hold back; the dashboard says "messages count as unchecked".
  Anything a conversation leads to is asked under the rule above. Muting the microphone is always
  free; anything else for `voice.mute` - unmuting, and any value that is not literally `true`
  (the field is a strict boolean) - follows the PIN rule. Which requests count as calm is pinned by a snapshot test
  (`tests/integration/test_lan.py`), so a state-changing `*.status` cannot slip in. A start needs a browser device linked to that node (`node.link`, else
  `link_required`) and is refused while the lock holds for it (`session_running`); a start
  without a readable report is followed by `computer.session_end`. Stops, kills and disconnects
  never lift the lock themselves - the node ends its session on `nox/kill` and its next report
  lifts it; for a node that went silent, the owner can lift the lock by hand on this machine with
  the PIN (`node.computer.release`, audited; a node still reporting a session locks again at
  once). While a session is open, `computer.pointer` and `computer.keys` are granted on that
  node only (`device:<id>|session:computer/*`, medium at most, `granted_by: node-session`); the
  grant and the header line ("Nox steuert gerade Maus und Tastatur auf ...", sent again to a
  dashboard that reconnects) follow the derived state. **No new input-synthesis code:** the node
  calls `nox.desktop.computer_use` and `nox.desktop.keyboard`, the two files the Security Model
  §10 CI guard allows, so the guard needs no exception. **Residual risks:** a remote-control tool
  that draws into the console session itself (Parsec, AnyDesk and the like) cannot be told apart
  from a person at the machine - the PIN is the guard there; node-local privacy zones are not
  applied to node screenshots yet (this core's privacy mode and capture switches are); a node
  without a desktop takes no camera picture at all (no visible indicator); a full-screen video
  also counts as "owns the screen" (refused, never the other way round).
- **Only the core's methods** (`CORE_TO_NODE`) are answered; a node never acts while disconnected
  and queues nothing.
- **Its own audit trail**: every decision, hash-chained, on the device (`nox node log`); the core
  cannot read or erase it. `nox/revoked` deletes the key and the certificate. The chain shows
  tampering, it does not prevent it: whoever controls the user's account can rewrite the whole
  file. A notice always starts with "Nox"; Windows PowerShell is started by its absolute path.
- **Self-update only from the signed release** (`nox.node.update`, docs/NODES.md#updates): the
  signature against the keys of the installed version (none configured: no update at all), no
  downgrade below the highest version that ran, **no replay** of a validly signed but older
  manifest (`created` must not go back), bundle size and SHA-256 from the manifest, every file
  against `BUNDLE.json`, the same `uv.lock`. It installs by itself only a stable, code-only patch
  whose node protocol the paired core accepts. **Freeze:** an attacker on the network can still
  withhold new manifests; after 30 days without a newer one the node says so (`nox node status`,
  a notice). **A taken-over node account** can write its own `app/` folder, so the launcher runs
  a self-installed version only after checking that folder at every start, with the installed
  program's code and keys, and every installer run drops those versions; a headless node's
  program (`/opt/nox-node`) is root's. The launcher, the tray and the trial start run Python
  isolated (`-I`, a trusted working folder, no `PYTHON*` variables); nothing elevated runs code
  from the user's folders (Windows Setup runs it as the original user and refuses node mode when
  elevated on purpose). **Accepted residual risks:** (a) a process a taken-over node account
  leaves running in the node's service cgroup (headless Linux) can change a release folder
  between the launcher's check and a later lazy import - surviving the launcher's own restarts,
  not `systemctl stop`, a reboot or a reinstall; (b) a taken-over node account can pin an older
  signed release that is still newer than the installed program via `state.json` until the next
  install.
- **Certificate renewal never strands a node:** the old certificate stays valid until the node
  first connects with the new one (then it is revoked) or ends by itself; a lost answer or a
  clock behind by more than an hour is retried after 10 minutes. Renewals are recorded in one
  database transaction under a per-node lock: concurrent requests get at most one certificate.
- **Residual risk, stated plainly:** "local" means the logged-in user. Malware running as that
  user with full user rights can read that user's keyring and files - the node key, the
  confirmation key - and act as the node towards the core within what the node may do. Nox's
  controls keep the *core* and the *AI* from widening a node; they do not defend a device whose
  user account is already compromised.

**Routing on the core** (`nox.node.registry`, `nox.node.router`): a node's tool list is
filtered (only tools the core routes, never a hard-prohibited name, at most 64) and its reported
risk can only raise the core's risk, never lower it. The device is resolved once, in the call's
permission scope and travels in its own field (`PermissionRequest.device`, `CallScope.device`,
the audit entry's `device`) - never parsed from the target, which can hold free text such as a
window title. A remembered grant is bound to that device (`device:<id>|<scope>`, written only from
that field; a grant on the core never covers a node and one for a node never covers another).
An `origin` default sends a side effect only to the node linked to the asking browser - offline
means "offline", never another device - and without a link Nox asks. Every revocation path - session end, a privacy change, the kill
switch, taint, an MCP server or skill removed - covers device-bound grants like the core's, and
removing a node revokes all of its grants. Side effects are never guessed onto a device (`needs_device`);
an offline device fails fast and is never substituted; nothing is queued. Results from a node are
untrusted: schema-checked, text only, bounded with a truncation marker, malformed answers become an
error, and the conversation is tainted. Device names are typed on the device, so they are
untrusted too: after NFKC only `A-Z a-z 0-9 ä ö ü Ä Ö Ü ß`, space and `- _ .` are allowed (no
look-alikes from other scripts, no control or bidi characters), at most 40, never a reserved word
such as "core" or "all", never shaped like a node id, and unique after case and umlaut folding.
A typed name that breaks a rule is refused before the code is spent - and only checked behind a
valid code, so nobody without one can probe which names exist. The model and the dashboard
always see the name with a short id the device cannot choose ("Laptop · 3f2a").

**The hands on a node** (C5, `nox.node.hands`, `nox.node.safe_fs`) - each one holds its own
boundary *after* `NodeGate`, the device's confirmation and the receipt:

- `file.list/read/write` open only inside folders confirmed on the device
  (`nox node allow files --folder`, none by default; an unconfirmed folder in `node.yaml` counts
  for nothing; never a drive root, the home folder or above, a system folder, an app-data root or
  a secret store). The text is checked first, unresolved (UNC and device paths, `..`, `~`, 8.3
  names, NUL, alternate data streams, Windows device names, trailing dots/spaces), then the
  **handle** decides: POSIX looks at every entry with `fstatat(AT_SYMLINK_NOFOLLOW)` before
  opening it `O_NOFOLLOW | O_NOCTTY | O_NONBLOCK` relative to the checked directory, and compares
  each directory with the protected ones by `(st_dev, st_ino)` (final path: `/proc/self/fd`,
  macOS `F_GETPATH`); Windows opens with `CreateFileW` without `FILE_SHARE_DELETE` and checks
  `GetFinalPathNameByHandleW` of that handle. Never inside the node's own folder or this user's
  secret stores (`secret_locations`: OS keyrings and credential vaults, DPAPI, `~/.ssh`,
  `~/.gnupg`, cloud CLIs, `.netrc`, `.pgpass`, `.git-credentials`, browser profiles), never a hard
  link to a file in them, never a FIFO, socket or device.
- **Writes are an allowlist of text formats in two tiers** (`safe_fs.write_refusal`), checked
  on the text and again on the final path relative to the allowed folder - `file.write` writes
  UTF-8 text, so nothing else is offered. PASSIVE (`.txt .md .csv .tsv .log .ics .vcf .srt`): a
  new file is medium. STRUCTURED (`.json .yaml .yml`): every write is high (device dialog,
  headless rules, never remembered), the file is written whole (append refused, so the check
  covers all of it), and the text must parse plainly (`content_refusal`: `json.loads`; YAML
  scanned for tags, anchors and aliases - refused - then `safe_load`). Documents, pages, PDFs,
  SVG and media (`NOT_WRITABLE_EXTENSIONS`) are refused (`format_not_writable`); there is no
  content scanner for them to bypass. Everywhere: no path component starting with `.`, no write
  at or below a folder holding a version-control marker (`.git .hg .svn .jj .bzr _darcs .sl` -
  `working_tree_refused`) or an app-workspace marker (`.obsidian .logseq logseq/config.edn .foam
  .dendron dendron.yml .vscode .idea` - `app_workspace_refused`: a vault runs `dataviewjs` and
  Templater from its notes; a dot-folder holding `app.json`, `appearance.json`,
  `community-plugins.json`, `core-plugins.json`, `workspace.json`, `plugins/` or `config.edn`
  marks a vault whose config folder was renamed), checked on each walked directory handle on
  POSIX and by name on the final path's parents **up to the drive root** - at the home folder and
  above generic markers do not count (`~/.vscode`, `~/.idea` and a bare `plugins/` are the apps'
  own settings), but version control and an unmistakable vault do (`.obsidian`, `.logseq`,
  `logseq/config.edn`, or a dot-folder with `app.json` and `workspace.json` or
  `core-plugins.json`); every entry of a folder is looked at, only dot-folders are opened, and a
  folder too full to read whole is refused rather than half-read -
  so a repository or vault around the allowed folder counts too; `nox node allow files --folder`
  refuses such a folder up front. Not detectable, and documented: an allowed folder linked
  (symlink, junction) *into* a vault from the vault's side. `.md` text is checked without
  parsing Markdown (a parser that differs from the note app's is a bypass) and with linear
  patterns only (no quantifier can overlap the next one, and no cap turns into a skip - an
  over-long code-block language or a fence followed by an unreadable character is refused; a
  1 MB adversarial text takes milliseconds): after `html.unescape`, NFKC and case folding, every run of three or more
  backticks or tildes - wherever it stands - must name no language or one of
  `INERT_FENCE_LANGUAGES` (text and data formats only: text, plaintext, txt, md, markdown, json,
  yaml, yml, toml, ini, csv, diff, log); a programming language (Execute Code and similar plugins
  run it on a click), a plugin's block or a rendered language (Mermaid, math/LaTeX, HTML, XML,
  CSS) is refused. As a further layer it is refused when it contains any of `dataviewjs`,
  `dataview-js`, `js-engine`, `templater`, `meta-bind-js`, `<%`, `<script`, `javascript:`,
  `vbscript:`, `obsidian://`, `file:`, `<iframe`, `<object`, `<embed`, inline Dataview JS (`$=`
  or `=this.` after a backtick or tilde), `%>` with Templater's `tp.`, or an `on...=` handler in
  a raw HTML tag - wherever it stands. Accepted false positive: Nox cannot write a note that
  merely mentions these plugin keywords. Every text write is capped at 256 KB, JSON/YAML at 64 KB (YAML is parsed
  with libyaml's `CSafeLoader` where available). An append to a
  `.md` file is judged on the whole file - read through the verified handle, refused when the
  file is over the cap - so it cannot be assembled in pieces, and the refusal says what blocks
  it. A nesting deep enough to exhaust a parser is `invalid_content`. All text checks run before
  anyone is asked and **off the event loop** with a 2 s limit (`tools.off_loop`). The text check is stateless
  (`NodeGate.precheck`: no lock, budget, receipt or audit), so a check that runs out of time is
  simply abandoned - it can never spend budget, consume a receipt or record an "allow" after its
  `check_timeout` refusal; the gate's stateful part then runs fast, under a lock. On the core the
  route's `prepare` runs the check before the permission scope; a
  write the node would refuse is never put before a person; no file without an allowed
  extension, and a config-name layer (`settings/tasks/launch/launchSettings/keybindings/package/
  project/tsconfig/jsconfig/composer/deno/flows.json`, `appsettings*.json`, `*compose*`,
  `lefthook*`, `*pipelines*` YAML, `Taskfile`, `configuration.yaml`, `automations.yaml`,
  `workflows/`). The execution-trigger denylist stays as a further layer. No write to a file with
  the executable bit or a second hard link; new files are `0600`; a path with leading or trailing
  spaces is refused, never trimmed. A new file is `CREATE_NEW`/`O_EXCL` and is removed again
  (through its handle, or by name in the checked directory) if it landed wrong; an existing one
  is truncated only after the check. The core's target and the device's dialog show path, mode,
  bytes, lines, a short SHA-256 and the content - in full up to 400 characters, otherwise the
  first and last 200 with "content shortened - N characters not shown". A started write
  completes; a stop ends everything after it. Accepted residual risk for PASSIVE formats: a
  `.csv` formula runs only after the spreadsheet's own prompt and a user action.
- **Profile rules and nodes**: a profile rule that ALLOWS applies to a call on a node only when it
  names that device exactly (`device:` with the full 32-hex id - `validate_profile` refuses a
  pattern for an ALLOW); rules that confirm or deny apply everywhere and may use a glob.
  A broad `tool: file*, decision: allow` therefore never lets Nox write on another PC unasked.
- Node file tools also never open Nox's own folder (`nox_home()`), in case a core runs on the same
  device. The protected places' identities (for the hard-link check) are cached for 30 s, and the
  known secret files of big stores (browser `Login Data`, `Cookies`, `Local State`, `key4.db`,
  `logins.json`, keys) are looked at before the bounded walk.
- **One renderer** (`protocol.render`) for the core's permission target and the device's dialog:
  JSON-quoted per value and per argv token, every character outside printable ASCII and Latin-1
  letters escaped (look-alikes, bidi, zero-width), runs of spaces counted. A target over 1000
  characters is refused, never shortened. A path alias is refused only in path fields; free text
  (a note, a shell line such as `git log HEAD~1`) is not a path.
- `shell.run` runs only in the device's own `make_sandbox()` with strong guarantees
  (`accept_weak` is never set on a node); the node's folder is not bound (bwrap) or granted
  (AppContainer; its owner-only ACL is re-applied on every start), and is refused as a runtime
  bind on both. On Linux the node fails closed when its folder, Nox's folder or a secret store
  lies under one of bwrap's read-only system binds. No sandbox (macOS, no bwrap, weak backend)
  means `sandbox_unavailable`, never an unconfined run. A stopped call (`nox/kill`, dead man)
  kills the sandboxed tree (bwrap: the cancelled run kills its cgroup and process; Windows: the
  job object). `package.*` is not offered on nodes: it could not work in the sandbox.
- A refusal by a hand is answered like the gate's and audited (`node.refused`). Residual: a
  Windows listing enumerates by the verified path while the folder's handle is held, with the
  folder's identity compared before and after.

Still to come in wave C: certificate renewal (C7) and a real presence check for
`nox node allow`. Negative tests: `tests/unit/node/` (a deliberately permissive core in `test_agent.py`),
`tests/integration/test_nodes.py`.

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
