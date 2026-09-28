> **Provenance.** Machine-assisted subsystem audit of the tree between `6625b50` and `3af0a5a` (read-only, code-reading
> with a few scratch experiments), written for [`../CAPABILITY_ANALYSIS.md`](../CAPABILITY_ANALYSIS.md).
> Line numbers may be off by a few lines for files changed since. Findings that the consolidated analysis relies on were re-checked
> by hand and are marked there; treat everything else here as a well-sourced lead, not a verdict.

# Audit F: UI, quality gates, delivery (Nox)

Audited at HEAD `ca878de` (branch `claude/nox-capability-analysis-v3-v4-abp5of`). The platform changes that were uncommitted when the audit started were committed during it; nothing was modified by this audit. Citations are `file:line`.

Commands actually run (read-only, or in the copy `<scratch>`):
- In the copy: `npm ci`, `vitest run`, `eslint .`, `tsc --noEmit`, `npm run build` and `npm audit`, for both apps.
- `scripts/gen_ts_types.py --check`, `scripts/secrets_scan.py --check`, `scripts/check_links.py`.
- `ruff` on plugins, scripts, ui/pet/scripts and installer.
- `mypy` on plugins and scripts, with its cache outside the repo.

The Python test suite was **not** run. `license_audit.py` was **not** run because it rewrites `docs/THIRD_PARTY_LICENSES.md`.

## 0. UI test results (run in the copy)

| Target | Result |
|---|---|
| ui/pet `vitest run` | **215/215 passed**, 12 files (9 pet + 3 shared), 5.2 s |
| ui/dashboard `vitest run` | **217/217 passed**, 12 files (9 dashboard + 3 shared), 8.0 s. The first run failed 1 file: `labels.test.ts:23` reads `../../src/nox/settings/schema.py`, so the UI tests only work inside the full repo layout. After I copied schema.py next to the copy, all passed. |
| eslint (both) | clean (exit 0). **Not run in CI.** |
| tsc --noEmit (both) | clean |
| vite build (both) | OK. pet 282 kB JS (90 kB gz), dashboard 354 kB JS (107 kB gz) |
| npm audit (both, incl. dev) | 0 vulnerabilities. **Not run in CI.** |
| gen_ts_types --check | up to date |
| secrets_scan --check | OK. 16 findings, 0 open. Note: RELEASE_CHECKLIST.md:47 still says 13. |
| check_links | OK, 17 md files. Only root, docs/ and .github/ are scanned (`scripts/check_links.py:16,36`). installer/README.md, ui/pet/scripts/README.md and plugin READMEs are not checked. |
| ruff check/format on plugins/, scripts/, ui/pet/scripts, installer, spikes | clean. **Not gated** (CI runs `ruff check src tests` only, ci.yml:42,45). |
| mypy --strict on plugins/*/src + scripts | 1 error, `scripts/bench_chat.py:633` (`TextIO.reconfigure`, union-attr). Plugins are clean. **Not gated** (CI runs `mypy src/nox` only, ci.yml:48,101). |

## 1. UI capability inventory

Maturity scale: WORKS+TESTED, WORKS-UNPROVEN-IN-REAL-ENV, LIMITED, SCAFFOLDING, MISSING.

The dashboard is a browser SPA served by the core at `/dashboard/`. It is opened from the tray: `shell/app.py:404-408` builds `dashboard_url` (`shell/logic.py:231`) with `#token=`. It has 8 tabs (`App.tsx:45-63`) whose panels stay mounted (`App.tsx:350-423`). The IPC surface is `ui/dashboard/src/ipc.ts:64-122`.

| Feature | What the user can do | IPC | Maturity | UI tests |
|---|---|---|---|---|
| Shell / nav / connection | Tabs (roving tabindex, Alt+1..8), theme switch, connection indicator, offline/no-token banners | `ipc.auth`, `ipc.subscribe` (shared/ipc.ts:122-160) | WORKS+TESTED for the client (envelope.test.ts, helpers.test.ts). App shell itself has no render test | none for `App.tsx` |
| Status page | Health/providers table, privacy-mode select+apply, assistant mode, mute, 2-step kill switch with reason | `state.get`, `health.get`, `ai.providers`, `privacy.set`, `mode.set`, `voice.mute`, `security.kill` | LIMITED. Privacy apply cannot reach FULL and cannot pass a PIN (see §2 H2). Kill works. There is no **resume** control although `security.resume` is allowed for UI roles (`ipc/handlers/core.py:178`) | Status.test.tsx (16) |
| **Chat** | Chat UI exists: streamed replies, provider byline, degraded marker, 3 example chips, clear, Ctrl+Enter | `chat.send` (stream, 60 s idle timeout, `model/chat.ts:67`) | WORKS+TESTED, and e2e-proven with the rules provider (`tests/e2e/test_dashboard_live.py:183-210`). No cancel/stop, no markdown rendering | Chat.test.tsx (10) |
| **Conversation history** | Only the in-memory transcript for the life of the tab (`App.tsx:104`). Lost on reload, core restart or a new tab. Nothing is fetched from the core; no history request exists | none | MISSING (server-side history view) | covered only as "survives tab switch" |
| Zuhause (Home Assistant) | Rooms/devices, toggle light/switch, run scene, one-sentence command, refresh | `home.status`, `home.list`, `home.light`, `home.switch`, `home.scene`, `home.command`; events `home.*` | WORKS+TESTED against a fake HA only. Real-HA integration test is `@network` and always skips (`tests/integration/test_home_plugin.py:283`) → WORKS-UNPROVEN-IN-REAL-ENV | Home.test.tsx (10) |
| Stream | Session status, OBS/Twitch plugin tiles, live Twitch chat feed (200 cap), Funken top-10, one-click "privacy scene" panic | `stream.session.status`, `stream.funken.top`, `security.panic`; events `stream.*`, `obs.*`, `twitch.*`, `plugin.*` | LIMITED. A plugin failure at boot (before the page connects) is never shown. `plugin.status` exists and is allowed (`dispatch.py:77`, `handlers/core.py:190`) but is not called; the comment at `dashboard/src/ipc.ts:34` still says it "would" help. Real OBS test always skips (`test_obs_plugin.py:177`) | model tests only; no Stream page render test |
| Clips | Review rail, tag edit, export to local `export_root`, refusal and "not installed" designed states | `clip.list`, `clip.tag`, `clip.export`; events `clip.*` | WORKS (refusal paths tested). Happy path is untested in the UI. `clip.trim` is not exposed | Refusals.test.tsx (2 clip tests) |
| Remote | Pair a phone (one-time code), list devices, unpair; off-state disables form | `remote.devices.list`, `remote.pair.start`, `remote.unpair` | WORKS-UNPROVEN-IN-REAL-ENV. `remote.enabled=false` by default | Refusals.test.tsx (2) |
| **Audit viewer** | Filterable table of `security.audit` events received on this connection only (500 cap, `model/state.ts:104`) | events only (`security.audit` is routed to dashboard/shell, `ipc/server.py:93`) | LIMITED. There is no read request for the append-only log, so everything before page load is invisible. The page says so honestly (`pages/Audit.tsx:2-5`) | **none** |
| Settings | Credentials first (Twitch device flow, OBS pw, Telegram token, HA token + connection test), PIN-carrying secret set/delete, 38 editable config paths (`settings/schema.py`), personality text, health-history table | `config.get/set`, `secrets.status/set/delete`, `security.pin.status`, `twitch.auth.*`, `personality.get/set`, `health.history`, `home.test` | WORKS+TESTED. Cross-checked against core allow-list by `labels.test.ts` | Settings.test.tsx (9), settings.test.ts (12), labels.test.ts (17) |
| **Plugin manager UI** | Only `plugins.enabled` as a config-form list field plus OBS/Twitch status tiles. No per-plugin status list, manifest/permission review, or install/remove | (`plugin.status` unused) | SCAFFOLDING / LIMITED | none |
| **Notification center** | Toasts only (`NotificationToasts.tsx`): max 5 shown, 20 kept in memory. Security and data-loss toasts are sticky. Dismiss is **local only**: the durable store's `proactive.notification.dismiss` is a tool, not an IPC request (`proactive/install.py:149-158`). No history, and nothing is re-fetched after a reload | events `proactive.notification` | LIMITED | model tests only; no Toasts render test |
| **Permission-confirm dialog** | Not in the dashboard. The native Qt dialog lives in the shell (`shell/dialogs.py:1`, `shell/app.py:311-367`). `security.permission.reply` is shell-only (`handlers/core.py:179`). A browser-only user can never approve a pending permission | n/a | MISSING in web UI (by design); shell dialog is WORKS-UNPROVEN-IN-REAL-ENV | none (UI) |
| **Memory browser** | None. `memory.*` IPC is limited to pet/remote/plugin (`ipc/server.py:87`); `memory.search/write` are tools only | none | MISSING | none |
| PM / coding sessions / creative / sensors & privacy zones | No UI. `pm.*`, `coding.*` events are not subscribed (`dashboard/src/ipc.ts:14-40`) | none | MISSING | none |
| **PIN enrolment** | No surface sets a PIN: `PinManager.set_pin` (`security/secrets.py:218`) is called only from tests. The UI only reads `security.pin.status`. The whole "PIN required after security kill" story (USER_GUIDE.md:68-70) cannot be enabled by a user | none | MISSING (UI, CLI and onboarding) | n/a |
| **Onboarding** | CLI only: `nox onboard` (`cli.py:88-94`, `onboarding/wizard.py`). No UI wizard. Vault/data paths are not editable in the UI (not in `EDITABLE_PATHS`) | n/a | CLI-ONLY | unit/onboarding (17) |
| Pet: renderer | Procedural Canvas2D variants (neutral default `config/defaults.yaml:162`; fox/cat/owl/imp); sprite sets (`sprite:<id>`); deformation rig (WebGL, Canvas2D fallback) for `sprite:meereswolf` | `pet.*`, `privacy.*`, `system.*`, `tts.*`, `voice.*`, `state.changed`, `settings.changed` (`pet/src/ipc.ts:18-26`) | Procedural: WORKS+TESTED (petRender, e2e canvas visible). Rig: logic TESTED (rigMath/rigFile/rigClips/rigRenderer/rigMeereswolf), **WebGL path WORKS-UNPROVEN-IN-REAL-ENV**: jsdom has no WebGL, and e2e uses the neutral variant | 215 tests, all logic-level; **no component render test** (App, RiggedPet, SpritePet, CaptureIndicator) |
| Pet: capture indicator | Always-mounted mic/screen/camera/cloud chip, letter+shape (not colour-only) | events | WORKS+TESTED (e2e `test_pet_capture_indicator_and_canvas`) | e2e only |
| Pet: click interaction | Click → `pet.interact` | `pet.interact` | WORKS | none |
| Pet: live variant swap | Intended via `state.get {path:'pet.variant'}` (`pet/src/App.tsx:77`) | `state.get` | **Dead path**: for role `pet` the core ignores `path` and returns only `assistant/privacy/system` (`ipc/handlers/core.py:62,204`), so it always falls back to the shell reloading the page | variants.test.ts (parser only) |
| Pet: OBS overlay (`?overlay=1`) | Transparent, no indicator | same | SCAFFOLDING. `pet_url(overlay=True)` is never called (`shell/logic.py:236-254`), no UI or doc gives the user the URL, and the token in it rotates every core start | none |

## 2. UX reliability findings

**H1 (High): session-token rotation strands the browser dashboard.**
- The core issues a new session token at every boot (`ipc/tokens.py:127`). The shell re-reads it (`shell/app.py:197`), but the dashboard lives in a browser tab with the old token only in memory (`shared/token.ts`, `dashboard/src/main.tsx:11`).
- After any core restart (supervisor crash-restart, update, kill → restart), the client reconnects with the stale token and gets `auth_failed`. It then sets `closedByUser=true` and never retries (`shared/ipc.ts:154-156,114`).
- The UI shows "authentication denied: invalid token" (`App.tsx:309`) **plus** the generic offline banner "values shown are the last known state" (`App.tsx:336-340`), which is misleading. Neither tells the user to reopen the dashboard from the tray.
- F5/reload is just as bad: the fragment was stripped, so the page lands on `no_token` (`App.tsx:331`).
- No e2e covers a restart.

**H2 (High): privacy mode from the dashboard silently fails or dead-ends.**
- `api.setPrivacy` sends only `{mode}` (`dashboard/src/ipc.ts:69`). It never sends `confirmed` or `pin`.
- Switching to FULL: the core returns without applying (`security/privacy.py:353-365`, `requires_confirmation=True`). `privacy_set` then returns the unchanged state dump (`handlers/core.py:244-257`), and the Status page treats that as success (`pages/Status.tsx:323-328`). The user picks FULL, clicks Apply, and nothing happens, with no message.
- With a PIN configured, any relaxing change raises `permission.denied` (`handlers/core.py:258-273`), and the Status page has no PIN input.
- USER_GUIDE.md:78-79 promises "moving back to FULL always asks for confirmation", but no such dialog exists in the dashboard.

**H3 (High, release trap): hardcoded UI client version.**
- Both UIs send `clientVersion: '0.1.0'` (`dashboard/src/ipc.ts:57`, `pet/src/ipc.ts:70`, default `shared/ipc.ts:127`).
- The hub denies any client whose major differs from `nox.__version__` (`ipc/server.py:599-603`).
- The first 1.x core will reject its own dashboard and pet. No test ties the two versions together.

**M1: UI language setting is ignored.**
- `identity.ui_language` is editable in Settings (`settings/schema.py:32,83`).
- The language is picked once from `?lang=` or the browser locale (`shared/lang.ts:12-19`, `dashboard/src/main.tsx:12`).
- `dashboard_url`/`pet_url` never append `lang` (`shell/logic.py:231-254`), so changing the setting has no visible effect.

**M2: untranslated core errors in the German UI.**
- `errorText` shows the core's or socket's English message verbatim, e.g. "Fehler: socket closed" or "chat.send timed out" (`shared/errors.ts:51-55`, `shared/ipc.ts:107,215`).
- Pet variant descriptions are German-only (`pet/src/variants/*.ts:14-19`).
- de/en key parity itself is enforced: 482 = 482 keys, `en.ts` is typed against `de.ts`, and `i18n.test.ts:6` checks it.

**M3: kill switch stays disabled after resume.**
- The kill phase `sent` is terminal until page reload (`model/chat.ts:57-61`, `pages/Status.tsx:132`).
- After the user resumes from tray or hotkey, the dashboard's kill button stays disabled. There is no resume button (see §1).

**M4: WebGL context loss is not handled.**
- There are no `webglcontextlost`/`restored` listeners in `pet/src/rig/renderer.ts` or `RiggedPet.tsx`.
- A GPU reset, driver update or sleep/resume can leave the rigged pet blank until the page reloads. Only `visibilitychange` is handled (`RiggedPet.tsx:158-179`).

**M5: notification dismiss is local only.**
- Dismissing a toast does not reach `nox.proactive.store`, so the durable store keeps it "active/unread".
- Toasts beyond 5 are hidden until others are dismissed (`NotificationToasts.tsx:36`).
- Notifications that arrive while the dashboard is closed are never shown.

**M6: plugin boot failures are invisible on the Stream tab** (see §1). Fix: call `plugin.status` on connect.

**M7: chat has no cancel and a 60 s idle timeout.**
- A long or hung generation can only be abandoned by waiting (`model/chat.ts:67`).
- Every stream delta maps and re-renders the entire unbounded transcript (`pages/Chat.tsx:66`, `App.tsx:104`), so cost is O(n) per token in long sessions. Nothing is memoised and the history is not capped.

**L1: settings loader can stay "loading".** If the client drops mid-load, the effect cleanup bumps the token and the stale load returns before `setLoading(false)` (`pages/settings/useSettingsData.ts` load/cleanup). "Loading" persists until reconnect.

**L2: every tab's load runs on every (re)connect.** Tab panels stay mounted, so connect fires a burst of about 12 requests, including Home/Remote/Clips/Settings loads the user may never open. This is within the hub's 50/s, burst-200 limit (`ipc/server.py:125-126`), so it is only wasteful.

**L3: no CSP or frame-ancestors on the UI HTTP routes.** Only `Referrer-Policy` and `nosniff` are set (`ipc/http.py:53-54`). This is hardening only: the token is needed to act.

**L4: the audit table is not virtualised.** It holds 500 rows and each event re-filters them all (`pages/Audit.tsx:61-72`). Fine at the current cap.

**Accessibility (positive).**
- Tablist has roving tabindex. There is a skip link, live regions are deliberately throttled (chat and audit announce once), the capture chip is not colour-only, and `prefers-reduced-motion` is honoured.
- eslint-plugin-jsx-a11y is configured and clean, **but lint is not in CI**, so regressions are ungated.

## 3. CI / quality-gate table

"Required" means listed in `scripts/github_branch_protection.ps1:53-60`. Branch protection is applied by that script only after publication (PUBLISHING.md step 5), so on the current private repo it is unverified.

| Job | Runner | Checks | Blocking? |
|---|---|---|---|
| `python` (ci.yml:21-60) | windows-latest | `uv sync --extra dev,shell,rl`; ruff check+format on `src tests`; `mypy src/nox` (strict); gen_ts_types --check; pytest unit (`not hardware/network/spike`); pytest integration (all markers; the 2 `@network` tests self-skip) | Yes, required |
| `python-posix` (ci.yml:62-107) | ubuntu-latest, macos-latest | mypy src/nox; unit; integration. **No ruff, no gen_ts_types.** Qt offscreen | Linux red fails the run, **but the job is not in the required-check list**, so it does not block merges. macOS is `continue-on-error` (ci.yml:67), non-blocking |
| `ui` pet/dashboard (ci.yml:109-136) | windows-latest | npm ci; `npm test` (vitest incl. shared); `npm run build` (tsc + vite). **No `npm run lint`, no `npm audit`** | Yes, required |
| `guard-security-model` (ci.yml:138-...) | windows-latest | grep for SendInput/pydirectinput/pyautogui/Read/WriteProcessMemory in `src/` and `plugins/rl/` | Yes, required |
| `release-hygiene` (ci.yml:163-203) | windows-latest | license_audit --check, secrets_scan --check (full history), upload license report | Yes, required. **The license audit only sees `--extra dev --extra shell` (ci.yml:180), so the voice extra the installer actually ships (`installer/requirements.lock` header: `--extra shell --extra voice`, including GPL piper-tts, faster-whisper, onnxruntime) is never audited in CI.** Comment at ci.yml:187-190 ("expected to be red") is stale: the piper exception exists in `docs/license_policy.yaml:78` |
| `e2e` (ci.yml:205-256) | windows-latest | builds both UIs, Playwright Chromium, `pytest tests/e2e -m e2e`: **2 tests** (dashboard auth + status + chat; pet indicator + canvas) | Yes, required |
| `branch-policy.yml` | ubuntu-latest | PRs into `main` must come from `develop` of the same repo | Header claims it is a required status (branch-policy.yml:3), **but it is not in the script's list**. PUBLISHING.md:192-193 says to add it manually later |
| `docs.yml` | ubuntu-latest | `check_links.py` (relative links) | Yes, required |
| Dependabot | n/a | weekly pip, npm x2, and github-actions **version** updates | advisory |

**Not gated:**
- **Dependency vulnerability scan.** No pip-audit, `npm audit` or osv in CI; RELEASE_CHECKLIST item 14 is open. npm is clean today (0 vulns).
- **Coverage threshold.** pytest-cov is a dev dependency, but there is no `--cov`/`fail_under` in `pyproject.toml [tool.pytest.ini_options]` and none in CI. Vitest has no coverage config either.
- **Type and lint gaps.** mypy is not run on `plugins/` (clean today) or `scripts/` (1 error today). Ruff does not cover `plugins/`, `scripts/`, `installer/` or `ui/pet/scripts` (all clean today).
- **UI lint and a11y lint.** `eslint` exists in both apps but CI never runs it.
- **e2e breadth.** 2 tests on Windows only. Nothing covers settings save, kill/resume, reconnect or token rotation, Qt shell, pet rig/WebGL, OBS overlay, installer, or macOS/Linux.
- **Other gaps:**
  - The `--extra voice` / `voice-kokoro` code paths never have their real deps installed in any CI job.
  - Installer build and smoke are not in CI.
  - Pushes to `develop` do not trigger CI (`on.push.branches: [main]` only, ci.yml:12-14); only PRs do.
  - A Security Model §11 suite runner does not exist (see §5).

## 4. Delivery / release readiness

**Installer state (`installer/`).**
- Two steps: `build.py` embeds Python 3.13, then Inno Setup `nox.iss`. It installs per-user to `%LOCALAPPDATA%\Programs\Nox` (`nox.iss:24,27`), unsigned (`installer/README.md` "Code-signing is not configured"), with no license page (no `LicenseFile`) and no `AppMutex`/`CloseApplications`. Upgrading over a running Nox will hit locked files, and there is no `[InstallDelete]`, so stale modules from older versions persist in `{app}\app\nox`.

**HIGH: the payload layout does not match runtime path resolution** (static analysis; not executed, no Windows host).
- `build.py:177-181` copies to `{app}\app\nox`, `{app}\app\config`, `{app}\app\plugins`, and `{app}\app\ui\pet` / `ui\dashboard` (no `dist`).
- `nox/paths.py:71-77` resolves the root as `PACKAGE_DIR.parents[1]` = `{app}` and looks for `{app}\config\defaults.yaml`. That does not exist, so it falls back to the package dir, and `CONFIG_DIR` becomes `{app}\app\nox\config`, which is missing.
- UI bundles are expected at `ui/pet/dist` and `ui/dashboard/dist` (`paths.py:85-86`). They are copied without `dist`, so the core silently serves no UI (`app.py:321`).
- `NOX_REPO_ROOT` is never set by the installer shortcuts (`nox.iss` `[Icons]`/`[Run]`).
- In addition, `python313._pth` gets only `import site` + `Lib\site-packages` (`build.py:81-91`), and nox itself is copied rather than pip-installed. Under `._pth` semantics (isolated/safe-path), `pythonw -m nox.supervisor` from `{app}\app` will very likely fail to import `nox` at all.
- The README's "observed ~261 MB for 0.2.0" shows a build was produced; nothing shows it was run. RELEASE_CHECKLIST item 7 (clean-machine install) is "not yet performed".

**No CLI in the installed build.** There is no `nox.exe` entry point and no `nox/__main__.py`, so an installed user cannot run `nox onboard`, `nox doctor` or `nox secrets set`, all of which the USER_GUIDE directs them to (USER_GUIDE.md:31,40,106).

**Other USER_GUIDE drift** (beyond the already-known signed-installer and update-channel claims):
- "installs under `%ProgramFiles%\Nox`" (USER_GUIDE.md:21); it actually goes to LOCALAPPDATA.
- "shows the project license" (:24); there is no LicenseFile.
- "installer's first-run step" runs onboarding (:31); there is none (`nox.iss` `[Code]` says so).
- "the pet's own menu" can trigger the kill switch (:67); the pet has no menu, and the shell has only a tray `QMenu` (`shell/tray.py:68`).
- "PM / coding assistant: planned, not part of this release" (:102); the coding and pm code ships.
- FULL-confirmation claim (:78-79); see H2.

**Update and rollback.** No updater, channel, or rollback code exists anywhere in `src/nox` (the grep only hits DB migrations). RELEASE_CHECKLIST item 8 is open, so this is untested and non-existent.

**Signing.** None: installer, `pythonw.exe` shortcuts and payload are all unsigned. SmartScreen will warn.

**Version consistency.**

| Source | Version |
|---|---|
| `pyproject.toml:3`, `src/nox/__init__.py:7` | 0.1.0.dev0 (what `nox --version` prints) |
| CHANGELOG.md:425 | [0.2.0] 2026-09-14 released |
| `installer/nox.iss:12` | 0.2.0, hardcoded, not derived |
| `ui/*/package.json:4`, UI `clientVersion` | 0.1.0 (see H3) |
| Git tags | none, local or remote. CHANGELOG compare links (:481-484) will 404 until PUBLISHING runs |

A stale comment at `pyproject.toml:82` says "Nox only supports Windows" while the classifiers now claim macOS and Linux.

**RELEASE_CHECKLIST.** 3 of 20 items are checked (1, 2, 17). Open:
- 3 and 4: the license audit is done locally but CI does not cover the voice extra.
- 5–9: onboarding, clean install, update/rollback, uninstall.
- 10: docs published.
- 11 and 18: external review, with no reviewer named.
- 12: §11 suite in release CI.
- 13 and 19: secrets scan re-run right before publishing.
- 14: vulnerability scan.
- 15: CI green on the public repo.
- 16: tags.
- 20: publication runbook.
- The PySide6 LGPL-replaceability sign-off is flagged open in `THIRD_PARTY_LICENSES.md` (Summary).

**v1.0 blockers (delivery):**
1. The installer payload/path mismatch (runs at all?).
2. Clean-VM install + onboarding.
3. The update/rollback mechanism (does not exist).
4. Code signing.
5. An installed-build CLI or UI onboarding.
6. PIN enrolment surface.
7. UI/core version coupling.
8. Tags and version unification.
9. The §11 suite, vulnerability scan and external review.

## 5. Test landscape

**Python** (static count of `def test_`; parametrize expands further):

| Area | Files | Test functions |
|---|---|---|
| unit | 192 | 1528 (+52 parametrize decorators) |
| integration | 19 | 81 |
| e2e | 1 | 2 |

Unit tests by package: plugins 324, voice 120, core 106, security 95, ai 95, ipc 90, memory 83, remote 62, settings 61, home 57, shell 52, rl 47, sensors 46, proactive 43, pm 39, clips 35, supervisor 32, data 31, stream 27, tools 25, creative 18, onboarding 17, health 14, **pet 8**.

**UI:** 215 (pet) + 217 (dashboard). Each total includes the same 30 shared tests, run twice.

**Thin or zero *direct* tests** (module never imported by any test; it may still be covered indirectly):
- nox: `entrypoints` (268 LOC), `home.targets` (404), `home.lexicon` (208), `security.audit_sink` (203, threaded queue behind every audit write; covered only indirectly via security service), `core.boot.workers` (174), `stream.booking` (131), `security.pin_attempts` (100), `proactive.dashboard` (80), `shell.tray` (156), `sensors.win32` (75), `util.proc`, `worker.heartbeat`, `plugins.reconnect`.
- Plugin modules with no direct unit test (exercised only via subprocess integration tests): `nox_plugin_home.plugin` (514), `nox_plugin_coding.session` (506), `nox_plugin_rl.plugin` (492), `nox_plugin_obs.plugin` (426), `nox_plugin_coding.plugin` (287), `nox_plugin_home.inventory` (204), `nox_plugin_clips.plugin` (136), `nox_plugin_coding.review` (87), `nox_plugin_telegram.ratelimit` (50).
- Packages with thin tests relative to size: `pet` (275 LOC / 8 tests), `health` (369 / 14), `stream` (631 / 27).
- UI with no render tests: dashboard `App.tsx`, Audit, Stream, NotificationToasts; the entire pet component layer (App, RiggedPet, SpritePet, CaptureIndicator). IpcClient reconnect/backoff is not tested; only `auth_failed` is (`shared/__tests__/envelope.test.ts:178`).

**Markers** (pyproject.toml:124-129): `spike` 2 uses, `hardware` 1, `network` 4 (2 in integration, both self-skip), `e2e` 1. There are 16 `skipif`s, mostly platform. The integration CI step does not deselect `network`.

**Flaky-risk patterns:**
- **Sleeps.** 155 `sleep(` calls in tests, 71 of them ≥0.1 s. Timing-based supervisor tests assert on heartbeat thresholds (`tests/unit/supervisor/test_supervisor.py:121,146,157,162,172,213`; sleeps of 0.3–1.0 s) and are the most likely to flake on the slower macOS/Linux runners. Also `tests/integration/test_rl_plugin.py:173` (1.0 s) and `tests/unit/ipc/test_client.py:128`.
- **Real ports.** 20 test files bind real loopback ports through `tests/_ports.py:15-31`, which probes and then releases the ports, a TOCTOU race under parallel runs.
- **Wall clock.** Only 1 `datetime.now`/`time.time` use. Timeouts are thread-method with 60 s default and up to 180 s per module.

**Security Model §11 negative-test suite:** it does **not** exist as a suite. There is no marker, no dedicated directory, and no CI step running it "as part of release CI" (RELEASE_CHECKLIST item 12 is open). The nine scenarios listed in docs/SECURITY.md:168-172 are covered piecemeal:

| Scenario | Coverage |
|---|---|
| Denied tool | `unit/security/test_permissions.py` |
| Config-removal bypass | `test_prohibitions.py:71` |
| PRIVATE + cloud | `test_permissions.py:106`, `test_egress.py:61`, `tools/test_executor.py:200` |
| Zone capture | `test_privacy.py:118` |
| Audit tamper | `test_audit.py:100,116` |
| Secret in log | `ipc/test_server.py:324`, `core/test_logging.py:68` |
| Unauthenticated IPC | `ipc/test_server.py:52-93` |
| Wrong role | `integration/test_security_resume.py:117` |
| Kill during streaming TTS | Only approximated: `voice/test_worker.py:146`. No test kills during an in-flight TTS stream |

EXTERNAL_SECURITY_REVIEW.md §4 requires a pasted run; none is in the repo.

## 6. Gaps and TODO/FIXME in scope

- No `TODO`/`FIXME`/`XXX`/`HACK` markers in ui/, scripts/, installer/, .github/, tests/ or the three in-scope docs. Debt is recorded in prose instead. Stale or misleading prose:
  - `dashboard/src/ipc.ts:34` ("See the report: a `plugin.status` read request would let the page ask"): the request exists.
  - `pages/Clips.tsx:7-9` says the dashboard role is refused `clip.list`, but the core registers it for UI roles (`clips/ipc.py:61`).
  - `.github/workflows/ci.yml:187-190`: license audit "expected to be red" (resolved by exception).
  - `branch-policy.yml:3`: "required status" is not in the protection script.
  - `pyproject.toml:82`: "Nox only supports Windows".
  - `RELEASE_CHECKLIST.md:47`: "13 findings"; the scan now reports 16.
  - `installer/README.md:3`: "signed-later".
  - `USER_GUIDE.md:21,24,31,67,78-79,102` (see §4).
- `pet/src/App.tsx:72-83`: live variant swap via `state.get` can never succeed for role `pet` (§1).
- `ui/dashboard/src/__tests__/labels.test.ts:23-25`: test depends on the Python source tree via a relative path.
- `scripts/bench_chat.py:633`: mypy error (ungated).
- `scripts/check_links.py:16`: only root, docs/ and .github/ markdown is scanned.
- `installer/build.py:177-181` vs `src/nox/paths.py:71-86`: layout mismatch (§4).

## 7. What the next versions need for UI/CI/delivery to be "reliable" (prioritised)

1. **Make the installed build start** (P0).
   - Either `pip install` nox into the embedded runtime, or write a `._pth`/`.pth` entry for `{app}\app`.
   - Set `NOX_REPO_ROOT` in the shortcuts, or align `build.py` to `ui/*/dist` + `config/` at the resolved root.
   - Add a CI job that builds the payload and runs a headless boot smoke (`pythonw -m nox.supervisor` → `/health` 200 → dashboard 200) on windows-latest.
2. **Couple UI and core versions** (P0). Inject `__version__` into the UI build (Vite `define`) or send the protocol version separately. Add a test asserting UI major == core major. Unify pyproject, CHANGELOG, `nox.iss` and `package.json` from one source, and create tags.
3. **Token-rotation recovery** (P0). On `auth_failed` or no-token, show "Open the dashboard again from the tray". Better: have the shell open or refresh the tab, or add a loopback-only token re-fetch endpoint gated by the shell. Add an e2e that restarts the core under an open dashboard.
4. **Privacy and PIN flows in UI** (P1).
   - Send `confirmed` after an explicit FULL confirm dialog.
   - Add a PIN prompt for relaxing changes and render `requires_confirmation`/applied from the response.
   - Add a PIN enrol/change surface (UI and CLI).
   - Add a Resume button; reset the kill phase when `system.level` leaves `safe_mode`.
5. **Close the CI gaps** (P1).
   - Run `npm run lint`, and `pip-audit` + `npm audit --audit-level=high`.
   - Extend ruff and mypy to `plugins/` and `scripts/`.
   - Put the `python-posix (ubuntu)` and branch-policy checks in the required list.
   - Run the license audit against a venv with `--extra voice`, matching what ships.
   - Add a coverage report with a floor, starting at the current baseline.
   - Trigger CI on pushes to `develop`.
6. **Create a real §11 suite** (P1). Add a `security_negative` marker on the 9 scenarios, add the missing "kill during streaming TTS" test, and run it as its own CI step whose output is archived for the external review.
7. **Broaden e2e** (P1). Settings save round-trip, kill + resume, reconnect, the notification toast, the rig variant (with Chromium's software WebGL), and the pet overlay. Run a minimal e2e on ubuntu too.
8. **Build the missing product surfaces** (P2).
   - Audit history read request plus paging.
   - Persistent conversation history (a core-side request) and chat cancel.
   - Notification center backed by the store, with dismiss via IPC.
   - Plugin status and manager page using `plugin.status`.
   - Memory browser (read-only first).
   - UI onboarding, or at least a first-run redirect to Settings, since the installed build has no CLI.
9. **Robustness** (P2).
   - WebGL context-loss handling.
   - Pass `lang` from `identity.ui_language` in shell URLs.
   - Translate core error codes.
   - Cap and memoise the chat transcript.
   - Render tests for Audit, Stream, Toasts, App and pet components.
   - Remove the pet `state.get` dead path or allow `pet.variant` for the pet role.
10. **Delivery hygiene** (P2).
    - Code signing, `AppMutex` + `CloseApplications`, `[InstallDelete]` for `{app}\app`, and `LicenseFile`.
    - An update mechanism with a rollback test (checklist item 8).
    - Fix the USER_GUIDE drift listed in §4.
