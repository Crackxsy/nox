# Plugin authoring guide

Audience: third-party plugin developers. A Nox plugin is **a manifest plus a worker process** — the
manifest is validated before anything is ever spawned, and everything the worker can do at runtime
is scoped to exactly what that manifest declared. Nothing a plugin does can widen what the core
already validated (`nox.plugins.manifest`, `nox.plugins.api`; ADR-012/ADR-013).

For a worked example, read `plugins/obs/manifest.yaml` and `plugins/obs/src/` (or the smaller
`plugins/echo/` for the minimal skeleton) alongside this guide.

## 1. Layout

```
plugins/<id>/
  manifest.yaml
  src/
    <package>/
      __init__.py        # exposes create(plugin_api) -> your plugin object
      ...
```

`<id>` must be lowercase, `[a-z][a-z0-9_]{1,31}`, and must match the directory name — the loader
rejects a mismatch before your code ever runs.

## 2. Manifest schema (`plugins/<id>/manifest.yaml`)

```yaml
id: myplugin
name: My Plugin
version: 0.1.0
api_version: 1                       # only 1 is supported today
entry: my_plugin_pkg:create          # "module.path:callable", called as create(plugin_api)
profiles: [stream]                   # security profiles this plugin runs under; empty = every profile
permissions:
  - tool: myplugin.do_thing          # must start with "<id>." and be dotted lowercase
    risk: medium                     # read | low | medium | high | critical
    preflight: myplugin.check        # optional: a read tool of yours the core asks first (§3)
  - tool: myplugin.check
    risk: read
events:
  emits: [myplugin.something_happened]
  listens: [security.panic]
requires:
  tools: [time.now]                  # tools of other components you call via plugin.tool.call
secrets: [nox/myplugin/api_key]      # must be "nox/<id>/..." - nothing else resolves
network:
  egress: ["api.example.com:443"]    # explicit host:port only, no wildcards
resources:
  memory_mb: 128
  priority: normal                   # low | normal | high
config:
  some_setting: 42                   # static config handed to you as plugin_api.config (never secrets)
```

Validated at load time (`nox.plugins.manifest.parse_manifest`) — a plugin that fails any of these
is never spawned:

- `id` matches its directory; `api_version` is one this runtime supports.
- `entry` matches `package.module:callable`.
- Every `permissions[].tool` starts with `<id>.`, is dotted lowercase, and is **not** one of the
  hard-prohibited names (`game.input.send`, `game.memory.read`, `game.process.inject`,
  `anticheat.bypass`, `stream.key.read`, `stream.stop`, `recording.delete`,
  `security.core.modify_without_pin`, `permission.self_elevate`) — declaring one of these fails
  validation outright, it is never merely denied at runtime.
- Every `events.emits`/`events.listens` entry is dotted lowercase. `emits` entries are exact names
  (no `*`) and never lie in a namespace the core publishes into: `privacy`, `security`, `system`,
  `sup`, `worker`, `ipc`, `plugin`, `voice`, `memory`.
- Every `requires.tools` entry is an exact tool name and not hard-prohibited.
- A `preflight` names a `read` tool the same manifest declares.
- Every `secrets[]` entry matches `nox/<id>/<key>` — a name outside your own `nox/<id>/` prefix
  fails validation.
- Every `network.egress[]` entry is `host:port` (no scheme wildcards, no bare hostnames) **and**
  is authorized against the *active security profile's* allow-lists by the core before your worker
  is spawned (`nox.plugins.manifest.authorize_egress`/`check_egress`) — loopback entries against
  the merged loopback allow-list, everything else against the profile's `egress_allowlist`.

## 3. The Plugin API (`PluginApi`, what `create(plugin_api)` receives)

One `PluginApi` instance per plugin worker process, built from your validated manifest:

- **`plugin_api.tools.register(name, input_model, handler, risk, *, description="", ...)`** —
  `name` must be inside your `<id>.` namespace and declared in `permissions`, with the exact
  `risk` your manifest declared (you cannot register a tool at a lower risk than declared).
  `input_model` is a pydantic `BaseModel` the core validates every call's payload against before
  your handler ever runs.
- **`plugin_api.events.emit(name, payload)`** — `name` must be in your manifest's `events.emits`.
  **`plugin_api.events.on(pattern, handler)`** — `pattern` must be in your `events.listens`.
  The core enforces both again on its side of the connection: the hub narrows your worker's
  subscriptions to `events.listens` plus the lifecycle events every worker gets
  (`security.kill_switch`, `security.panic`, `privacy.mode_changed`, `privacy.capture_changed`,
  `system.stopping`), and refuses - and audits - any event you send that is not an exact
  `events.emits` name. Skipping the API gains nothing.
- **`plugin.tool.call`** (the `plugin.tool.call` IPC request) — calls a tool of another component,
  but only one listed under `requires.tools`. It runs through the core's `ToolExecutor` exactly like
  a user's or the model's call: validation, the permission engine (a `confirm` decision asks the
  user), the kill switch, the timeout, and an audit entry with `plugin:<id>` as the actor.
- **Preflight** — when a tool of yours can reach further than its input says (a scene changes other
  entities, a relay may open a gate), give it a `preflight:` read tool. Before every call the core
  sends that tool `{"tool": <name>, "input": <validated input>}` and expects
  `{"decision": "allow" | "confirm" | "deny", "targets": [...], "reason": "..."}`. `deny` refuses
  the call, `confirm` turns an allow into a confirmation that names the `targets`, and an error or
  timeout refuses it. A preflight can only make the decision stricter. `plugins/home` is the
  worked example (`home.effect`).
- **`plugin_api.state.get(path=None)`** — a read-only view of the non-private state tree (the core
  filters what your role can see).
- **`plugin_api.secrets.get(name)`** — `name` must be one of your declared `nox/<id>/...` secrets;
  the core resolves it from the keyring, the value is never persisted in your process beyond the
  call.
- **`plugin_api.http(**kwargs)`** — an `httpx.AsyncClient` whose every request passes your
  manifest-scoped `EgressGuard` (`PluginEgressGuard`): an endpoint not in your own
  `network.egress` is denied *before* the inherited privacy-mode rules (OFFLINE/PRIVATE → allow-
  listed loopback only) even run. Raises `PluginApiError` at construction if your manifest
  declares no `network.egress` at all. Before opening a raw socket or WebSocket yourself, call
  `plugin_api.egress.authorize(host, port, scheme=...)`: every decision it makes - allowed or
  denied - is reported to the core (`plugin.egress.report`) and lands in the audit log with your
  plugin as the actor.
- **`plugin_api.privacy.mode`** — the worker's live copy of the core's current privacy mode; your
  own egress guard consults it on every request.
- **`plugin_api.config`** — your manifest's static `config:` block (never secrets).
- **`plugin_api.log`** — a `structlog` logger namespaced `nox.plugin.<id>`; the same "never log
  secrets/transcripts/memory content at INFO" rule applies to plugin code.

Every one of these calls raises `PluginApiError` (a `RuntimeError`) if you ask for something your
manifest did not declare — there is no silent fallback. Events, tool calls and secrets are also
enforced by the core, so bypassing the API does not widen them. Two things are not: a plugin
worker is an ordinary process, so it can open a socket without asking its egress guard (nothing is
then reported or audited) and read any file its user can. That is why a plugin runs only when you
list it in `plugins.enabled`, and why review (§6) reads the code, not only the manifest.

## 4. Packaging

- Your worker is spawned as its own process by `nox.plugins.manager.PluginManager` — write it like
  any other Python entry point; it does not share memory with the core.
- Declare real dependencies in your own package (`pyproject.toml`/equivalent inside your plugin's
  `src/`) — Nox does not currently vendor third-party plugin dependencies into the core venv for
  you; that packaging story is not built yet, so don't assume it.
- Ship tests alongside your plugin the same way the built-in `obs`/`twitch` plugins do
  (`tests/unit/plugins/<id>/`, `tests/integration/test_<id>_plugin.py`) — a fake/in-process double
  for whatever external service you talk to (see `plugins/obs`'s `FakeObsServer` pattern), never a
  test that requires a real external account or credential.
- **Never** commit a real secret, token, or credential anywhere in your plugin's source or test
  fixtures — tests must use an in-memory secret backend, exactly like the core's own tests do.

## 5. Design guidance from the shipped plugins

- Keep your tool surface **narrow and named for exactly what it does** — `plugins/obs/manifest.yaml`
  deliberately has no `obs.scene.delete`, `obs.scene.rename`, or `obs.source.delete` tool at all,
  on top of those being the kind of action the hard-prohibition/profile-risk system would gate
  anyway. Don't expose a generic "run arbitrary command" tool.
- Prefer `risk: read` for anything that only observes, and reserve `medium`/`high` for anything
  with a real side effect — the core's default-by-risk rule (§ permission model, `SECURITY.md`)
  means `medium`+ already asks the user to confirm, so an honestly-scoped risk level is part of
  your plugin's actual safety story, not paperwork.
- If your plugin talks to a local service (OBS, a local game, etc.), declare only that exact
  `127.0.0.1:<port>` in `network.egress` — never a wildcard, never `0.0.0.0`.

## 6. How a plugin is reviewed and approved

There is no public plugin marketplace yet, and no third-party plugin ecosystem tooling has shipped.
A plugin runs only if it is listed in `plugins.enabled` in your own config — nothing is
auto-discovered or auto-enabled from a directory drop-in alone. For a plugin you intend to
contribute back to the Nox repository itself, open a pull request; review focuses on exactly the
manifest boundaries above (namespace, hard prohibitions, secrets, egress) plus normal code review
— see `CHANGELOG.md`'s changelog-review-before-merge process and `SECURITY.md` for what a reviewer
checks.
