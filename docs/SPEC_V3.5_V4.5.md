# Specification: Nox v3.5 – v4.5

Status: **proposal**, for the maintainer to accept, change or reject. Grounded in
[`CAPABILITY_ANALYSIS.md`](CAPABILITY_ANALYSIS.md); finding IDs (G1, R3, D4, …) refer to its
register.

> **Kurzfassung (Deutsch).** Drei Wellen, jede mit einem einzigen Ziel und einem messbaren
> Abschluss-Gate:
>
> | Welle | Leitsatz | Kern | Abschluss, wenn … |
> | --- | --- | --- | --- |
> | **v3.5 „Wahrheit"** | Was Nox behauptet, stimmt. | PIN einrichtbar; Profile wirklich durchgesetzt; Zustand übersteht Neustarts; Worker kommen nach Absturz zurück; Datenlöschung läuft; Doku ohne Übertreibung | jede Garantie aus SECURITY/PRIVACY hat einen Negativtest in CI und ist im Code wahr |
> | **v4.0 „Handeln"** | Nox kann etwas tun, nicht nur reden. | Agent-Schleife: Modell wählt Werkzeug → Permission-Engine prüft → Ergebnis zurück → weiter. Erinnerungen, echtes Gedächtnis, alle vorhandenen Tools erreichbar | eine deutsche Aufgaben-Suite wird zu ≥ 90 % korrekt erledigt und **keine** Aktion mit Seiteneffekt passiert ohne Freigabe |
> | **v4.5 „Beweis"** | Es hält im echten Leben. | Echte Dienste in CI (Home Assistant im Container), Soak- und Chaos-Tests, Installer mit Update/Rollback, externes Security-Review | die v1.0-Release-Checkliste ist vollständig mit Belegen abgehakt |
>
> „Alles können" ist bewusst **kein** Ziel. Wachstum kommt aus *einem* kontrollierten Mechanismus
> (Werkzeuge unter der Permission-Engine), nicht aus Einzel-Features. Was Nox nie tun soll (Eingaben
> in Spiele, Türen öffnen, Stream stoppen), bleibt ausgeschlossen.

## 1. Versions

Nox has shipped `0.1.0` and `0.2.0`; "v2" and "v3" were development waves on the way to the
unreleased `0.3`. This specification keeps the wave names the project uses and maps them onto
releases:

| Wave | Release | Theme |
| --- | --- | --- |
| v3 (done, #48) + cross-platform (this branch) | part of `0.3.0` | home control, rig, latency, Windows/macOS/Linux |
| **v3.5** | `0.3.0` | Truth - every claim holds |
| **v4.0** | `0.4.0` | Agency - Nox can act, under the permission engine |
| **v4.5** | `0.5.0` → `1.0.0-rc` | Proof - real environments, delivery, review |

One version number for everything: `pyproject.toml`, `nox.__version__`, both UIs'
`clientVersion` (generated, not hand-written) and the installer are the same value (fixes R9).

## 2. What "reliable" means here

A capability counts as delivered only when all five hold. Each work item below names the one it
closes.

| # | Property | Test that proves it |
| --- | --- | --- |
| P1 | **Reachable**: a user can get to it from a UI, voice or chat | an e2e or integration test drives it from the user surface |
| P2 | **Permitted below the model**: the decision is made in code, and a denied path is refused | a negative test that tries the forbidden variant |
| P3 | **Observable**: health, the dashboard and `nox doctor` say whether it works, and why not | a test asserts the `limited`/`unavailable` reason |
| P4 | **Recoverable**: survives the component crashing, reconnecting, restarting and the machine rebooting, without becoming *less* safe | a fault-injection test |
| P5 | **Proven for real**: exercised against the real service or hardware at least once per release | a recorded run in the real-environment lab (v4.5) or the release log |

## 3. Principles that do not change

- Security and privacy decisions stay below the model, in code (ENGINEERING.md hard rules).
- Fail closed: "cannot tell" is never "allowed" (the `unobservable` zone, the unreadable PIN store).
- State is **level-triggered at connect**: every consumer asks for the current state when it
  starts or reconnects; events only carry changes after that.
- No fake capabilities: the model is never told it can do something it cannot.
- Permanent non-goals: input synthesis or memory access for games; unlocking doors, disarming
  alarms, opening valves or garages; stopping a stream or deleting OBS scenes; telemetry.

## 4. v3.5 - Truth

**Goal:** every statement in README, SECURITY.md, PRIVACY.md, USER_GUIDE.md and the system prompt is
true, and stays true across crashes and restarts.

### 4.1 Work items

| ID | Requirement | Acceptance criteria | Closes |
| --- | --- | --- | --- |
| T-01 | **PIN setup** | `nox onboard` and the dashboard Settings page can set, change and remove the PIN (removing needs the old one). `argon2-cffi` becomes a dependency; existing PBKDF2 hashes are verified and re-hashed to Argon2id on next success. A raw value stored under `nox/security/pin` is detected and reported, never silently accepted. | G1, G10, D3 · P1 P2 P3 |
| T-02 | **Profiles enforced** | The router, memory writes and plugin spawning consult the active profile's `cloud_allowed`, `memory_writes_allowed`, `integrations_allowed` *and* the privacy mode. Test matrix: every shipped profile × every privacy mode × {chat, escalation, memory write, plugin start} asserts the outcome. | G2, D6 · P2 |
| T-03 | **Exact loopback** | `is_loopback` parses with `ipaddress`; names are loopback only if they are `localhost`. The HTTP and WebSocket servers check `Host` and `Origin` (DNS-rebinding). Negative tests: `127.evil.example`, `localhost.evil`, rebinding `Host`. | G3 · P2 |
| T-04 | **Level-triggered state everywhere** | Plugins, the RL capture gate, the orchestrator and the UIs fetch the effective privacy/capture/kill state on connect (the voice worker already does). The orchestrator's safe mode reads the kill switch, not `system.started`. Test per consumer: state set before it connects is honoured. | G4, G5 · P4 |
| T-05 | **State survives restarts** | Privacy mode, panic and kill state are persisted and restored at boot. If the stored state cannot be read, boot uses the strictest of {stored, configured}. | G6 · P4 |
| T-06 | **Tamper-evident means complete** | The audit head (sequence number + hash) is anchored outside the database (runtime file + keyring). Tail truncation, checkpoint rollback and a deleted database are detected at boot and engage safe mode. A failed audit write for a side effect refuses the side effect. | G7 · P2 P4 |
| T-07 | **Plugin boundary in the core** | The core, not the plugin, filters a plugin's subscriptions and emitted event namespaces (`privacy.*`, `security.*`, `system.*` reserved); `plugin.tool.call` only reaches tools the manifest declares and is audited; plugin egress is audited. | G8 · P2 |
| T-08 | **Local identity** | Each child gets a spawn-time secret; the supervisor stops trusting a self-reported pid. Shell, pet and dashboard get separate tokens with fixed roles. A single-instance lock guards supervisor and core. | G9, R10 · P2 |
| T-09 | **Home boundary by effect** | Scenes, scripts and automations whose Home Assistant definition touches a forbidden domain are refused or confirm-gated with the entity shown; `switch.*`/`cover` without a safe `device_class` are confirm-gated. | G11 · P2 |
| T-10 | **Zones cannot be switched off by accident** | `sensors.enabled: false` keeps foreground sensing for zones (or enters the fail-closed zone); only an explicit, PIN-gated `privacy.zones_enabled: false` disables zones. | G12 · P2 |
| T-11 | **Workers come back** | Workers get a reconnect credential valid for their process lifetime; the core respawns a crashed voice worker with backoff and names the failure (missing model path, device) in health; a plugin that loses the hub reconnects or exits (never a zombie). | R1, R5 · P3 P4 |
| T-12 | **Safe mode has a way out** | Tray, dashboard and shell can resume through the supervisor (`sup.resume`), PIN-gated for security-path kills; the dashboard gets a Resume button and the kill button stays usable after resume. | R2 · P1 P4 |
| T-13 | **Retention runs** | A scheduled job purges every retention-bound table on the configured schedule; health reports the last successful run; PRIVACY.md lists each table with its retention. | R3, D2 · P3 |
| T-14 | **Data survives damage** | Backup before every migration (kept N copies in `backups_dir`); a damaged header or failed integrity check renames the file aside and boots with an empty DB, loudly; `user.yaml` written atomically and a single bad key rejects only that key. | R4 · P4 |
| T-15 | **Cheap, honest health** | Audit verification is incremental and off the event loop; integrity check is scheduled (not every 30 s); provider health never sends a paid request - the Claude Code probe checks CLI presence and login only - and nothing probes a cloud provider while privacy forbids cloud. | R6, R7 · P3 |
| T-16 | **Voice does what it says** | Barge-in cancels queued sentences; follow-ups inside the conversation window are addressed to Nox; kill phrase matching tolerates filler words and German/English spelling, and is ignored while Nox itself is speaking (no self-trigger); capture and wake-gate health reach the core; no model is downloaded without the user asking. | R8, D4, D5 · P1 P3 |
| T-17 | **UIs survive the core** | `clientVersion` generated from the package version; on token rotation the dashboard and pet re-read the token (or say exactly how to reopen) instead of failing permanently; privacy-mode FULL shows the confirmation and PIN field; conversation history persisted and shown. | R9 · P1 P4 |
| T-18 | **Say only what is true** | USER_GUIDE, README, SECURITY, PRIVACY and the system prompt are corrected (D1–D7). The prompt lists only capabilities available *now* in the active profile. `.hc.py` and dead configuration keys are removed or wired. | D1–D7 · P3 |

### 4.2 Exit gate

- Every row of the guarantee table in SECURITY.md has at least one negative test, collected in one
  `tests/security/` suite (the "§11 suite") that CI runs as a required check.
- Fault-injection tests exist for: voice worker killed, plugin killed, hub connection dropped, core
  killed, supervisor killed (orphans must end within 15 s), database header damaged, `user.yaml`
  with one invalid key.
- CI: Linux and macOS jobs are required checks; `pip-audit` and `npm audit` gate on high/critical;
  ESLint runs in CI; statement coverage does not drop below the level measured at the start of v3.5.
- The documentation-truth checklist (D1–D7) is closed.

### 4.3 Out of scope for v3.5

New features. The only additions are the setup and recovery paths needed to make existing claims
true (PIN setup, resume, conversation history).

## 5. v4.0 - Agency

**Goal:** Nox can carry out tasks, not only answer, through one mechanism: a bounded agent loop in
which the model proposes tool calls and the existing `ToolExecutor` (validation → permission engine
→ confirmation → timeout → audit) decides and executes them.

### 5.1 Work items

| ID | Requirement | Acceptance criteria | Closes |
| --- | --- | --- | --- |
| A-01 | **Agent loop** | The orchestrator runs propose → execute → observe → continue, bounded by a step limit, a time budget and a token budget, cancellable at every step (barge-in, kill switch). Every model-originated call goes through `ToolExecutor.call` with `origin=model`. | P1 P2 |
| A-02 | **Tool calling per provider** | Ollama receives the `tools` array from `ToolRegistry.describe()` and its `tool_calls` are executed; the Claude Code provider reaches Nox tools only through a local MCP bridge that proxies to `ToolExecutor` (no direct tool access of its own); the rules provider maps deterministic intents (home, timers, time/date) onto the same tools. | P1 P2 |
| A-03 | **Tool results are untrusted** | Every tool result and every retrieved note enters the context wrapped as untrusted data. Content that came from untrusted sources (chat, Telegram, vault, web, entity names) marks the turn as tainted; a tainted turn can read, but every side effect requires explicit user confirmation. Red-team suite of injection cases: 0 unconfirmed side effects. | P2 |
| A-04 | **Confirmation everywhere the user is** | Model-originated confirmations appear in the pet, the dashboard, the Qt dialog and by voice ("Soll ich … ? Ja / Nein"), always naming the target entity or path; plugins can request confirmation through the core. | P1 P2 |
| A-05 | **No claimed action without a result** | A turn that says an action happened must reference a successful tool result; otherwise the reply is rewritten to "Das konnte ich nicht tun, weil …". Evaluated in the task suite. | P2 P3 |
| A-06 | **Reminders and timers** | "Erinnere mich in 20 Minuten an …", "stell einen Timer", "um 18 Uhr" create persistent tasks (existing `TaskQueue`); they fire through `ProactiveService` with privacy and quiet-hours gating, survive restarts, and are listed and cancellable in the dashboard. | P1 P3 P4 |
| A-07 | **Memory that remembers** | "Merk dir …" stores a memory item; conversation history persists across sessions; retrieval covers memory items and vault; failed embeddings are retried; a dashboard memory browser can list, correct and delete; retention and privacy zones apply. | P1 P3 |
| A-08 | **Existing capabilities made reachable** | Home intents from voice and chat; coding sessions asynchronous (start returns an id, progress by event); OBS read tools and the confirmed scene switch; RL replay summaries; clip tools. Every plugin tool either has a caller or is removed. | P1 |
| A-09 | **Profile-scoped tool sets** | The tool list offered to the model is filtered by the active profile and privacy mode before the model sees it; the permission engine still decides at call time. | P2 |

### 5.2 Exit gate

- **Task suite**: at least 150 scripted German tasks (home, timers, memory, lookups, stream, coding
  hand-off), run in CI against the rules provider and nightly against a pinned local Ollama model:
  ≥ 90 % completed correctly; 100 % of side effects confirmed or pre-allowed by the profile; 0
  claimed-but-not-performed actions.
- **Injection suite**: at least 50 cases across Twitch, Telegram, vault notes and entity names: 0
  unconfirmed side effects, 0 secrets or zoned content in any outbound request.
- **Latency budgets** (measured with `scripts/bench_chat.py`, reference machine documented): time to
  first token and time to first spoken sentence no worse than the v3 baseline for non-tool turns;
  a single-tool turn completes within 3 s locally, excluding confirmation wait.

### 5.3 Out of scope for v4.0

Browser automation, desktop control (keyboard/mouse synthesis), mail and calendar write access,
payments. Candidates for later waves, each behind its own threat model; read-only local calendar
is the first candidate once A-03 has held for a release.

## 6. v4.5 - Proof

**Goal:** what v3.5 made true and v4.0 made capable is shown to hold in real environments and can be
installed, updated and rolled back by someone who is not a developer.

### 6.1 Work items

| ID | Requirement | Acceptance criteria | Closes |
| --- | --- | --- | --- |
| V-01 | **Real-environment lab** | Home Assistant runs in a container in CI and the home plugin's integration tests run against it (including a large installation fixture beyond the 1 MiB frame limit); OBS and Twitch run nightly against a test instance/channel; Telegram against a test bot. Results are published per release. | P5 |
| V-02 | **Hardware voice protocol** | A written, repeatable test protocol (quiet room, background video, game audio, USB mic unplug/replug, speakers without headset) with recorded false-wake, missed-wake, kill-phrase and barge-in rates, run on Windows and one Linux and one macOS machine per release. | P5 |
| V-03 | **Wake word and echo** | A trained "Nox" wake-word model ships (licence-compatible); speaker playback does not self-trigger (echo cancellation or a half-duplex fallback that is reported as such). | P5 |
| V-04 | **Soak and chaos** | 72-hour soak with periodic fault injection (worker kills, hub drops, core kills, disk nearly full, clock jumps) on Windows and Linux: no leaked processes, memory growth under a stated bound, every fault recovered within its stated time, no state becoming less private. | P4 P5 |
| V-05 | **Installer, update, rollback** | Installer layout fixed and booted in CI; signed binaries; update with smoke test and automatic rollback under injected failure; uninstall separates program and data and never deletes the vault (release checklist items 7–9). macOS and Linux: documented source install via `uv tool install` - packages are a later decision. | P1 P4 P5 |
| V-06 | **External security review** | Scope from `EXTERNAL_SECURITY_REVIEW.md` delivered to a named reviewer; all critical/high findings fixed or explicitly accepted with a reason (release checklist 11, 18). | P2 P5 |
| V-07 | **Licences settled** | The bundled voice stack is licence-clean for the chosen distribution terms (release checklist 3–4), or the GPL route is chosen deliberately and stated. | - |
| V-08 | **Accessibility and language** | WCAG AA in both UIs checked with an automated tool in CI plus a manual keyboard-only pass; every user-visible core error string is translated; the interface-language setting takes effect. | P1 |
| V-09 | **Diagnostics without telemetry** | `nox doctor --bundle` writes a redacted local diagnostics archive (health history, versions, recent logs without content) the user can choose to attach to an issue. | P3 |

### 6.2 Exit gate

`docs/RELEASE_CHECKLIST.md` complete with evidence for every item, the soak report and lab results
attached to the release, and the external review closed → tag `1.0.0-rc1`.

## 7. Cross-cutting rules for all three waves

- Every work item lands with its tests in the same pull request; a fixed bug comes with a test that
  fails before the fix (as on this branch).
- Every user-visible change gets a CHANGELOG line; every changed guarantee updates SECURITY.md or
  PRIVACY.md in the same pull request.
- A work item is "done" only when all of its listed properties (P1–P5) have their evidence.
- Items may move between waves; the exit gates may not be weakened without writing down why.

## 8. Decisions the maintainer has to make

These are product decisions, not engineering ones. Each has a recommendation.

| # | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| 1 | Microphone under Wayland (today: closed by the fail-closed zone) | keep closed · allow an explicit, PIN-gated opt-in that exempts only the microphone | Opt-in, PIN-gated, off by default, shown permanently in the pet status. Screen and memory stay closed. |
| 2 | How the cloud model uses tools (A-02) | Claude Code CLI + local MCP bridge · direct Anthropic API with a key in the credential store | MCP bridge: keeps the existing "no API key of our own" model and puts every call through `ToolExecutor`. |
| 3 | Funken economy | build it out (spend, earn events) · reduce to what works and delete dead config | Reduce now (v3.5), revisit when streaming is a focus again. |
| 4 | Rocket League coaching | invest in real HUD recognition/model · freeze at replay summaries | Freeze at replay summaries until v4.5; the current HUD stage is scaffolding. |
| 5 | Release numbering | as in §1 · keep wave names only | As in §1: waves stay the working names, releases follow SemVer. |
| 6 | Platforms for the installer | Windows only · also macOS/Linux packages | Windows only in v4.5; macOS/Linux stay source installs until someone owns them. |

## 9. Risks

| Risk | Consequence | Mitigation |
| --- | --- | --- |
| The agent loop (v4.0) widens the attack surface through prompt injection | a chat message or note triggers a side effect | A-03 taint rule and confirmation, injection suite as an exit gate, profile-scoped tool sets |
| Local models are too weak for reliable tool selection | the task-suite target is missed | deterministic intents first (rules provider), measured model choice, cloud escalation only where privacy allows |
| v3.5 looks like "no progress" to users | pressure to skip to features | v3.5 visibly adds PIN setup, a Resume button and conversation history - the prerequisites that are also user-facing |
| No Windows/macOS hardware in the loop for contributors | regressions found late | required CI on all three OSes (v3.5), lab and soak on real machines (v4.5) |
| Single maintainer | the plan stalls | waves are independent enough to ship partially; each work item is a self-contained pull request |
