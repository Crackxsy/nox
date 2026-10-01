# Getting started

Nox on your machine in four steps. If something does not work, `nox doctor` is the first thing to
run - it checks the machine rather than guessing.

## 1. Install

**There is no signed installer yet.** The Inno Setup script exists and builds one
(`installer/build.py`), but nobody has signed or proven it, so the honest route today is from
source. This section will change when that changes.

You need:

- **Windows 11.** Nox is Windows-only by design.
- **Python 3.13 from python.org** - *not* the Microsoft Store build, which sandboxes paths.
- **Node.js 20+**, to build the two web UIs the core serves.
- Optional: **Ollama** for local models, **Claude Code CLI** for cloud-grade reasoning.
  With Ollama, pull the model Nox expects: `ollama pull qwen3:4b-instruct`. Which model you
  use decides whether Nox can *act* or only talk - see [`docs/LOCAL_MODELS.md`](LOCAL_MODELS.md).

```powershell
git clone https://github.com/Crackxsy/nox.git
cd nox
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

That installs the dependencies, builds both web UIs and runs the machine check. It stops at the
first failure and says which step failed. It deliberately does not start Nox or run the wizard -
both ask questions.

By hand, if you would rather see each step:

```powershell
python -m pip install uv
uv sync --extra dev --extra shell
cd ui\pet ; npm ci ; npm run build ; cd ..\..
cd ui\dashboard ; npm ci ; npm run build ; cd ..\..
```

The UIs have to be built: the core serves the built files, not a dev server. Forgetting this is the
usual reason for a pet window that renders nothing.

## 2. Check the machine

```powershell
.venv\Scripts\nox doctor
```

It reports Python, your configuration, the folders it will use, which AI backends are actually
reachable, and whether the speech extras are installed. Every line is a real check, not a guess: a
backend that is not there says so.

## 3. Set it up

```powershell
.venv\Scripts\nox onboard
```

The wizard asks for a name, a language, where your data and vault folders go, microphone and camera
consent (separately, and never silently), and which AI backend to use - probing both before you
choose. Every step is skippable and every answer can be changed by running it again.

It writes only to `%APPDATA%\Nox\user.yaml`. Secrets never go in there; they go to the Windows
Credential Manager, via `nox secrets set <name>`.

## 4. Start it

```powershell
.venv\Scripts\nox supervisor
```

That is the real thing: the supervisor starts the core and the desktop shell, restarts the core if
it dies, and owns the kill switch. The pet appears; the dashboard is at the address the console
prints.

For development, `nox dev` runs the core and shell in one console without the supervisor - easier to
read, no restart-on-crash.

## A shortcut on your desktop

The installer offers one (ticked by default). From a source checkout, make one by hand:

1. Right-click the desktop → **New** → **Shortcut**.
2. Target - adjust the path to your checkout:
   ```
   C:\Users\<you>\nox\.venv\Scripts\pythonw.exe -m nox.supervisor
   ```
   `pythonw.exe` rather than `python.exe`: no console window.
3. **Start in**: your checkout folder, e.g. `C:\Users\<you>\nox`.
4. Name it Nox.

To start it with Windows, put that same shortcut in
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` (press Win+R and type `shell:startup`).

## What to try first

- Say or type **"was kannst du gerade?"** - Nox answers from a catalogue, not from a guess, and it
  will tell you what it *cannot* do.
- Ask it for something it can see: **"welche fenster sind offen?"**
- Give it a folder (`files.roots` in the settings) and ask **"was liegt in meinen Downloads?"**
- Ask for a picture of something: **"zeig mir den Speicherverbrauch als Balkendiagramm"** - it
  appears on the Board page.
- Shift-click the creature to give it a treat.

Nothing dangerous is on by default. There are no folders in `files.roots`, no presets, no
self-extension workspace - a fresh Nox can look at very little until you say otherwise, and it says
which setting to change when you ask for something it cannot reach.

## When something is wrong

| Symptom | First thing to check |
| --- | --- |
| It will not start | `nox doctor`. It names the missing piece. |
| The pet window is blank | The UIs were not built - step 1's `npm run build`, both of them. |
| No voice | The `voice` extra and its models: `uv sync --extra voice`, then `nox doctor`. |
| "no folder is configured" | That is the file boundary doing its job. Add one to `files.roots`. |
| "the coding plugin is not running" | Self-extension needs the coding profile. Switch to it. |

Logs are under your data folder (`%APPDATA%\Nox\logs` by default). They never contain secrets.
