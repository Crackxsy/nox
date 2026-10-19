# Nox installer

Two-step pipeline: `build.py` assembles a self-contained, embedded-Python payload; `nox.iss`
(Inno Setup) turns that payload into a signed-later, per-user installer.

## 1. Build the payload

```
python installer/build.py
```

(Run with the **system** Python, not `.venv\Scripts\python.exe` - `uv` lives on the system
Python per `docs/ENGINEERING.md`.)

What it does, in order:

1. Downloads the official Python 3.13 embeddable zip (pinned version, matches the dev venv's
   3.13.14) into `installer/.cache/` and unpacks it to `build/runtime/`.
2. Fixes `build/runtime/python313._pth` (`import site` uncommented, `Lib\site-packages` added) -
   the embeddable distribution ships with both disabled, which would make a plain `pip install`
   invisible to the interpreter.
3. Bootstraps `pip` into that runtime via `get-pip.py` (the embeddable zip has no pip).
4. Re-exports `installer/requirements.lock` via `uv export --extra shell --extra voice
   --no-hashes` and `pip install`s everything in it *except* the `-e .` self-reference (nox's own
   source is copied, not pip-installed - the payload never needs a build backend). Pass
   `--skip-lock-export` to reuse the committed lock file instead of re-resolving.
5. Copies `src/nox`, `config/`, `plugins/`, `ui/pet/dist`, `ui/dashboard/dist` into `build/app/`.
   (The two dashboards must already be built: `cd ui/pet && npm run build`, same for
   `ui/dashboard`.)
6. Writes `build/app/BUILD_INFO.txt` with the measured runtime/app/total size against the
   "< 400 MB without models" packaging target.

Useful flags: `--skip-download` (reuse an already-unpacked `build/runtime/` while iterating),
`--python-version X.Y.Z` (override the pinned embeddable version).

## 2. Compile the installer

Requires Inno Setup 6 (`winget install JRSoftware.InnoSetup` - installs per-user, no admin
needed; ends up at `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`).

```
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\nox.iss
```

Output: `installer/out/Nox-Setup-<version>.exe` (git-ignored, like `installer/build/` - see
`.gitignore`; `build/` at the repo root, where `build.py` actually writes, was already ignored).

## What the installer does

- Installs to `%LOCALAPPDATA%\Programs\Nox` (per-user, `PrivilegesRequired=lowest` - no UAC
  prompt, no admin rights needed).
- Start-menu shortcut running `runtime\pythonw.exe "{app}\app\start_nox.pyw"` - the launcher
  puts `{app}\app` on `sys.path` itself and, if Nox cannot start, writes the reason to
  `%LOCALAPPDATA%\Nox\startup-error.log` and shows it in a message box.
- Optional (unchecked by default) autostart shortcut in the user's Startup folder - never turned
  on without the user explicitly checking the box during setup.
- **Never touches the user's vault, database or data folders, or any other data path.**
  Nothing outside `{app}` (the install directory) is written or read by the installer itself; the
  vault path, name, language and optional Twitch/mic-camera/AI-backend choices are all handled by
  nox's own first-start flow, not by Inno Setup.
- Uninstall removes only `{app}\runtime` and `{app}\app` (program files) - the vault and all other
  user data are left exactly as they were.
- No telemetry, no network access by the installer itself (everything it packages was already
  fetched by `build.py` beforehand).

## Repairing an installation that does not start

Installers built before this fix (0.2.0 - 0.5.0) produce a copy that never starts: no window, no
tray icon, no log. Two mistakes in the build caused it:

- The embedded Python's `runtime\python313._pth` is its whole `sys.path` - not even the working
  directory is added - and it did not name `app\`, where the `nox` package lives. The shortcuts
  ran `pythonw.exe -m nox.supervisor`, which failed on `import nox`; `pythonw` has no console to
  print that to.
- `nox.paths` looked for `config\defaults.yaml` one folder above `app\`, so even an importable
  copy would not have found its configuration.

To repair:

1. Quit any Nox that is running from a source checkout, if you use one.
2. Uninstall the old copy: **Settings → Apps → Installed apps → Nox → Uninstall**. This removes
   only the program files (`runtime\` and `app\` in the install folder). Your vault, database,
   user configuration (`%APPDATA%\Nox\user.yaml`) and data folder are not touched.
3. Build and run a new installer (sections 1 and 2 above) and install. It recreates the
   Start-menu and desktop shortcuts and - if you tick it - the autostart entry, all pointing at
   `start_nox.pyw`.
4. Start Nox from the Start menu. If it still does not come up, the reason is in
   `%LOCALAPPDATA%\Nox\startup-error.log`.

Editing the old copy's `._pth` by hand is not enough: its code has the second mistake too.
Until a new installer is built, run Nox from a source checkout instead
(`docs/GETTING_STARTED.md`) and point the autostart shortcut at that checkout's
`.venv\Scripts\pythonw.exe -m nox.supervisor`; remove the old autostart entry first
(Win+R, `shell:startup`, delete the old "Nox" shortcut), or Windows keeps trying the dead copy.

## Known sizes and limitations

- A real build produces `build/app/` + `build/runtime/` at roughly runtime 1 GB, app a few MB,
  uncompressed total ~1 GB (see `build/app/BUILD_INFO.txt` after running `build.py`). Compiled
  through Inno Setup with lzma2/max compression, the installer itself comes out well under the
  "< 400 MB without models" target (observed: ~261 MB for version 0.2.0).
- **pip isolation matters**: on a machine that also has a system Python of the same
  version/architecture, an embedded-runtime `pip install` that isn't isolated can silently write
  into that system Python's per-user site-packages instead of the embedded runtime. `build.py`
  sets `PYTHONNOUSERSITE=1` on every subprocess call that touches the embedded interpreter's pip
  specifically to prevent this - see `_isolated_env()` in `build.py`. If you ever see packages
  appearing in your own per-user site-packages after running this script, that guard is the first
  place to check.
- Not yet done: a full clean-VM validation pass (a clean Windows 11 VM with no prior
  Python/Visual C++/Nox artifacts, repeated installs, an end-to-end smoke test, and
  uninstall-cleanliness verification). A machine that already has development tooling installed
  cannot stand in for that test.
- Code-signing is not configured in `nox.iss` - `Nox-Setup-*.exe` from this pipeline is unsigned.
  That is a separate, tracked piece of work.
