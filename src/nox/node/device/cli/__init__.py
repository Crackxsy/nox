"""`nox-node pair | run | status | log | allow | deny | ...` - the node's own terminal commands.

Everything here runs as the logged-in user and touches only that user's node folder
(`nox.node.device.home`). `allow` is the one way to widen what Nox may do on this device; nothing
the core sends can do it. While Nox controls mouse and keyboard here, every command that widens
something refuses (`session_refusal`), so Nox cannot type one into a terminal of this PC. The words
follow this terminal's language and name the command as this device needs it
(`nox.node.device.cli_text`).

The package is cut by what a command is for:

* `runtime` - what every command shares: `NodeCliError`, the session refusal, the secret store,
  the audit log and `build`, which wires a node together.
* `pairing` - `pair` and the questions asked when a device is first confirmed.
* `serve` - `run`, the node's main loop.
* `updates` - the daily update check inside `run`, and `nox-node update`.
* `controls` - `status`, `log`, `stop` and `resume`.
* `allow` - `allow` and `deny` (one function, `allow(..., enabled)`): the one way to widen what
  Nox may do here, and `games`.
"""
