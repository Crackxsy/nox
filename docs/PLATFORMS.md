# Platforms

Nox runs on **Windows 11**, **macOS** and **Linux**. Windows 11 is the primary platform: it is
where the product is used day to day, where every CI job runs, and the only platform with an
installer. On macOS and Linux Nox runs from source, and a few capabilities depend on what the
desktop is willing to tell an application - where it will not, Nox says so and stays on the safe
side instead of pretending.

This page is the honest per-platform picture. `nox doctor` and the dashboard's health page show
the same thing for the machine in front of you.

## At a glance

✅ works and is tested · ◐ implemented and tested off-screen or with fakes, not yet confirmed on a
real desktop of that kind · ❌ not possible on that platform · – not applicable

| Capability | Windows 11 | macOS | Linux (X11 session) | Linux (Wayland session) |
| --- | --- | --- | --- | --- |
| Core, IPC, dashboard, AI routing, memory | ✅ | ◐ | ✅ | ✅ |
| Desktop shell (pet window, tray) | ✅ | ◐ Qt | ◐ Qt | ◐ Qt (stay-on-top is up to the compositor) |
| Click-through pet | ✅ | ◐ | ◐ | ◐ |
| **Privacy zones** (banking, password manager, …) | ✅ | ◐ with the Accessibility permission, otherwise **fail closed** | ◐ needs `xprop` | ❌ **always fail closed** |
| Idle / away detection | ✅ | ◐ (`ioreg`) | ◐ needs `xprintidle` | ❌ not available |
| Kill-switch **hotkey** | ✅ | ◐ with the Input Monitoring permission | ◐ | ❌ tray or dashboard only |
| Credential store | Credential Manager | Keychain | Secret Service (GNOME Keyring, KWallet) | Secret Service |
| Orphan protection (child processes end with their parent) | job object (kernel) | parent watch | parent watch | parent watch |
| Voice (Whisper, Piper/Kokoro) | ✅ | ◐ untested on real hardware | ◐ untested on real hardware | ❌ microphone closed by the fail-closed zone (see below) |
| Rocket League coach | ✅ | – (no native game build) | – | – |
| Installer, update, rollback | not proven yet | none - run from source | none - run from source | none - run from source |
| CI | every job, blocking | unit + integration, **not yet blocking** | unit + integration, blocking | (same as X11 in CI) |

## What "fail closed" means for privacy zones

A privacy zone switches screen capture, screenshots, clipboard reads and memory writes off while a
sensitive window - online banking, a password manager, a private chat - is in front. To know that,
Nox has to read the title of the active window.

When it cannot, it does **not** assume that nothing sensitive is open. It enters the reserved
`unobservable` zone, which closes every gate a real zone closes, and the `sensors` health check
reports `limited` with the reason. Concretely:

* **Wayland** does not let any application read the active window. Under a Wayland session the
  zone is permanently active: Nox works as a typed companion and assistant in the dashboard, but
  it does not listen, does not capture the screen and does not write memories - a zone closes the
  microphone too, so there is no voice and no spoken kill phrase; the kill switch is the tray and
  the dashboard. Log in to an X11 session ("GNOME on Xorg", "Plasma (X11)") if you want those.
  Whether a user may knowingly exempt the microphone from this is an open design question
  (tracked in the v3.5 specification), not something Nox decides on its own.
* **macOS** only reveals another app's window title to a process with the **Accessibility**
  permission (System Settings → Privacy & Security → Accessibility → add the terminal or Python
  that runs Nox). Until it is granted, the zone stays active and health names the missing
  permission.
* **Linux X11** needs `xprop` (package `x11-utils` on Debian/Ubuntu, `xorg-xprop` on Arch). Idle
  detection additionally needs `xprintidle`.
* A machine without any graphical session (a server, a container) is unobservable too.

The voice worker takes the current state from the core every time it connects, not only from
later changes, so a zone that was already in force when it started keeps the microphone closed.

A process pattern still wins where it matches - a password manager identified by its process name
is reported as the `password_manager` zone even when its window title cannot be read.

## Where Nox keeps its files

One folder per installation holds `user.yaml`, the data folder and the runtime files:

| Platform | Default location |
| --- | --- |
| Windows | `%APPDATA%\Nox` |
| macOS | `~/Library/Application Support/Nox` |
| Linux | `$XDG_DATA_HOME/nox`, i.e. `~/.local/share/nox` by default |

Set `NOX_APP_DIR` to put it anywhere else (a portable install, a second profile). In
configuration files the same location is written `${NOX_APP_DIR}`; the shipped defaults use it, so
they resolve correctly on every platform. A `user.yaml` written on Windows that still says
`${APPDATA}` keeps loading elsewhere - the variable falls back to `~/AppData/Roaming` - but on
macOS and Linux you will want to change it.

## Install on macOS or Linux

```bash
git clone https://github.com/Crackxsy/nox.git
cd nox

python3.13 -m pip install uv
python3.13 -m uv sync --extra dev --extra shell --extra voice

(cd ui/pet && npm ci && npm run build)
(cd ui/dashboard && npm ci && npm run build)

.venv/bin/python -m nox.cli onboard
.venv/bin/python -m nox.cli doctor
.venv/bin/python -m nox.cli supervisor
```

Linux prerequisites, Debian/Ubuntu names: `x11-utils` and `xprintidle` (desktop signals),
`libportaudio2` (voice), and a running Secret Service (GNOME Keyring or KWallet - every mainstream
desktop session starts one). Without a Secret Service Nox still starts, reports the `secrets`
health check as unavailable, and refuses every change that would relax security, because it can
no longer tell whether a PIN is set.

## Process containment

On Windows every process Nox starts is placed in a job object that the kernel closes - ending the
whole tree - when its owner dies. macOS and Linux have no common equivalent, so each spawner passes
its own process id as `NOX_PARENT_PID` and every child watches that exact process (pid and start
time). A child whose parent is gone sends itself SIGTERM, which the core turns into a clean
shutdown, and exits hard if it is still alive ten seconds later. A crashed supervisor therefore
cannot leave a core, a voice worker or a plugin running unsupervised - with a microphone, a screen
capture or a chat connection still open.
