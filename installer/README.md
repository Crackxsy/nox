# Nox installer (SP-16, ST-10-01/02)

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
6. Writes `build/app/BUILD_INFO.txt` with the measured runtime/app/total size against the SP-16
   "< 400 MB without models" target (FR-15.4).

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
- Start-menu shortcut running `runtime\pythonw.exe -m nox.supervisor`.
- Optional (unchecked by default) autostart shortcut in the user's Startup folder - never turned
  on without the user explicitly checking the box during setup.
- **Never touches `E:\Nox\vault`, `E:\Nox\database`, `E:\Nox\data`, or any other data path.**
  Nothing outside `{app}` (the install directory) is written or read by the installer itself; the
  vault path, name, language and optional Twitch/mic-camera/AI-backend choices are all handled by
  nox's own first-start flow (ST-10-03), not by Inno Setup.
- Uninstall removes only `{app}\runtime` and `{app}\app` (program files) - the vault and all other
  user data are left exactly as they were (FR-15.4).
- No telemetry, no network access by the installer itself (everything it packages was already
  fetched by `build.py` beforehand).

## Honest status (2026-09-14, this pass)

- `build.py` was run for real on the dev machine (`E:\Nox\repo`) and produced `build/app/` +
  `build/runtime/`: runtime 1006.5 MB, app 1.7 MB, uncompressed total 1008.1 MB
  (`build/app/BUILD_INFO.txt`).
- Inno Setup 6.7.3 was installed unattended via `winget install --id JRSoftware.InnoSetup -e
  --silent` (lands at `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`, no admin needed) and
  `ISCC.exe installer\nox.iss` compiled successfully against that payload:
  `installer/out/Nox-Setup-0.2.0.exe`, **261 MB** - under the "< 400 MB without models" target
  despite the >1 GB uncompressed payload (lzma2/max compression).
- **Incident found and fixed during this run**: the first build attempt did not isolate pip from
  the OS-standard per-user site-packages path, which - because of identical version/arch tags -
  coincides with this machine's system Python's own per-user packages. 20 packages were
  uninstalled from that shared location before the fix (`PYTHONNOUSERSITE=1` on every subprocess
  call touching the embedded interpreter's pip); all 20 were restored to their exact prior
  versions by hand, and a second from-scratch rebuild verified zero drift
  (`pip list --user --format=freeze` diffed clean before/after). See `SP-16`'s Result section in
  the vault and `build.py`'s `_isolated_env()` docstring.
- **Not done in this pass, and not possible on this machine**: SP-16's actual validation
  protocol - a clean Windows 11 VM with no prior Python/Visual C++/Nox artifacts, three repeated
  installs, the walking-skeleton smoke test, and uninstall-cleanliness verification. This dev
  machine already has Python, VS Code, Git and other tooling installed, so it cannot stand in for
  a clean-VM test; SP-16 stays open until someone runs that protocol on an actual clean VM (Hyper-V
  or equivalent) per the spike note's "Test setup" section. Record the result there, not here.
- Code-signing (SP-16 / ST-10-04's "self-signed dev channel is sufficient for v0.5") is not
  configured in `nox.iss` - `Nox-Setup-*.exe` from this pipeline is unsigned. That is a separate
  story (ST-10-04) and out of scope for this build/install-script pass.
