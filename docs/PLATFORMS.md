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
| Kill-switch **hotkey** | ✅ | ◐ with the Input Monitoring permission | ◐ | ❌ tray, dashboard, or the spoken kill phrase with continuous listening |
| Credential store | Credential Manager | Keychain | Secret Service (GNOME Keyring, KWallet) | Secret Service |
| Orphan protection (child processes end with their parent) | job object (kernel) | parent watch | parent watch | parent watch |
| Voice (Whisper, Piper/Kokoro) | ✅ | ◐ untested on real hardware | ◐ untested on real hardware | ◐ continuous listening only - push-to-talk is a global hotkey, which Wayland forbids (see below) |
| Rocket League coach | ✅ | – (no native game build) | – | – |
| Installer, update, rollback | not proven yet | none - run from source | none - run from source | none - run from source |
| CI | every job, blocking | unit + integration, **not yet blocking** | unit + integration, blocking | (same as X11 in CI) |

## What "fail closed" means for privacy zones

A privacy zone switches the microphone, screen capture, the camera, screenshots, clipboard reads
and memory writes off while a sensitive window - online banking, a password manager, a private
chat - is in front. To know that, Nox has to read the title of the active window.

When it cannot, it does **not** assume that nothing sensitive is open. It enters the reserved
`unobservable` zone, and the `sensors` health check reports `limited` with the reason. What that
zone closes is `privacy.unobservable_policy`:

* `screen_only` (the default): everything that looks at the screen stays closed - screen capture,
  the camera, screenshots to the cloud, clipboard reads - while the microphone and memory writes
  keep working. An unseen window can only leak through what is on the screen, not through what
  you say.
* `strict`: every gate a real zone closes, the microphone and memory writes included.

A real zone - one Nox *can* see, or a password manager recognised by its process name - always
closes everything, under either setting. Concretely:

* **Wayland** does not let any application read the active window. Under a Wayland session the
  `unobservable` zone is permanently active: Nox never captures the screen there. With the default
  `screen_only` it listens and remembers - but only with `voice.stt.listening_mode: continuous`,
  because push-to-talk is a global hotkey and Wayland lets no application register one. In that
  mode the spoken kill phrase ("Nox, Notaus") works; otherwise the kill switch is the tray and the
  dashboard. With `strict` there is no voice at all. Log in to an X11 session ("GNOME on Xorg",
  "Plasma (X11)") for push-to-talk, the kill hotkey and window-aware zones.
* **macOS** only reveals another app's window title to a process with the **Accessibility**
  permission (System Settings → Privacy & Security → Accessibility → add the terminal or Python
  that runs Nox). Until it is granted, the zone stays active and health names the missing
  permission.
* **Linux X11** needs `xprop` (package `x11-utils` on Debian/Ubuntu, `xorg-xprop` on Arch). Idle
  detection additionally needs `xprintidle`.
* A machine without any graphical session (a server, a container) is unobservable too.

The voice worker takes the current state from the core every time it connects, not only from
later changes, so a zone that was already in force when it started keeps the microphone closed.

Zones do not depend on the other desktop sensors: with `sensors.enabled: false` Nox still checks
the window in front for zones (and records nothing else). Only `privacy.zones_enabled: false`, a
PIN-gated setting, switches window zones off, and the `sensors` health check then says so.

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
