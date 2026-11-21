"""The node's eyes and the rest of its hands: `screen.capture`, `camera.snapshot`,
`browser.*`, `desktop.*`, `computer.*` and `media.control` - the core's tool names, run on this
device under this device's rules.

Each is reached only through `NodeGate` (prohibitions, the game lock, stop state, the group this
device enabled - all off by default except `media` -, risk ceiling, the core's receipt, budget,
and the dialog on this device for high risk). On top of that:

* **Captures** (`screen.capture`, `camera.snapshot`) are high risk here: the person at this
  device says yes to every single one, in the dialog on this device. Before the picture is taken,
  a notification on this device says that Nox is taking it; when that notice cannot be shown, the
  capture is refused (`no_visible_indicator`) - never a silent picture. The picture is held in
  memory for this call only and goes to the core that asked as `nox/blob` chunks; nothing is
  written on this device.
* **`browser.*`** is Nox's own headless Chromium with a profile of its own under the node's
  folder, never the user's browser; every request goes through `NetGuard` (no LAN, no loopback),
  as on the core. `browser.screenshot` is not offered here (it would keep a file on the device).
* **`desktop.*`** and **`computer.*`** are Windows only; elsewhere every call is refused
  (`windows_only`). They are the core's own modules, unchanged: the boundary (`nox.desktop
  .boundary`: Windows itself, Nox, the game), the game guard inside `ComputerUse` before every
  single event, the self-approval lock (no input into Nox's windows or while a dialog is open), the
  stop hotkey. A session is opened only by `computer.session_start` (high risk: the dialog on this
  device, every session) and ends with a stop, a lost connection or the dead-man rule. No input
  synthesis happens in this module: it calls `nox.desktop.computer_use`, one of the two files the
  CI guard (Security Model §10) allows to synthesise input. While a session runs, the node keeps
  a marker in its folder (`SESSION_MARKER`): every terminal command that widens what Nox may do
  here refuses while it says a session runs - or cannot be read (`session_marker_state`), so Nox
  cannot type `nox-node allow ...` into a terminal of the PC it controls. Terminal windows whose
  process tree runs Nox count as Nox's own windows too (`terminal_runs_nox`): no click or key
  goes into them.
* **The game lock** (`GameLock`): `NodeGate` refuses mouse, keys and typing while a watched game
  or an anti-cheat service runs here or a game is in front, before anything else is looked at.
  `game.input.send` stays a hard prohibition.
* **`media.control`** (low): play, pause, next, previous, volume, mute - SMTC and Core Audio on
  Windows, MPRIS (`playerctl` or D-Bus) and PipeWire/PulseAudio on Linux. The action is one word
  of a fixed list and the level a number; neither ever becomes script text.
* **Only what can work is offered** (`world_tools`): a tool whose library is missing on this device
  (`mss` for the screen, OpenCV for the camera - the extra `senses` -, Playwright for the browser -
  the extra `browser`), `desktop.*` and `computer.*` off Windows and `media.control` where there
  is no backend (macOS) are not in the node's list at all, so neither the core's status nor the
  dashboard ever claims them. What can change while the node runs - a display that comes or
  goes, Chromium not downloaded yet - is still refused per call with its reason.

The imports inside functions in this package are deliberate. They are not cycle workarounds: what
they load is Windows-only (`nox.desktop.win32`, the anti-cheat and process probes), belongs to an
optional extra a device may not have installed (`senses`: mss and OpenCV, `browser`: Playwright)
or pulls in a heavy stack that a node which never uses the tool should not pay for. A device
without them still starts and says so (`node_world.installed`).

The package is cut along the parts named above:

* `notices` - what a person at the device is told, and the marker file of a computer-use session.
* `game_lock` - the game lock.
* `media` - `media.control` on each platform.
* `node_world` - `World`: what the node's captures, browser and desktop tools reach, behind
  injectable seams.
* `catalogue` - `world_tools`, the tools a device offers, built on a `World`.
"""
