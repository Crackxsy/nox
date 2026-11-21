"""The node's hands: `file.list/read/write/move/delete` and `shell.run` - the core's tool names,
run on this device under this device's rules.

They run only after `NodeGate` allowed the call (prohibitions, stop state, capability group,
risk ceiling, arguments, the core's receipt, budget, and the confirmation on this device for high
risk). On top of that, each one holds its own boundary at the moment it acts:

* **`file.*`** open every path through `nox.node.device.safe_fs`: only inside a folder this device
  allowed (`nox node allow files --folder <path>`, none by default, never a whole drive, the home
  folder or a system folder), decided on the real file at open time, never inside the node's own
  folder or this user's secret stores. A write never lands where it could make something run
  outside the sandbox (`.git`, startup/autostart folders, shell start files, scripts and
  programs), never on an executable file or one with a second hard link, never overwrites unless
  asked and never creates folders. Replacing or appending is high risk (asked on the device);
  a new file is medium. A write that has started completes - a stop ends everything after it.
* **`file.move`** and **`file.delete`** are high risk (asked on the device every time, never
  remembered) and follow the same rules for both ends of a move and for the file deleted
  (`safe_fs.move_file`, `safe_fs.trash_file`): files only, never a link, a move never replaces
  anything, never creates a folder and never crosses drives; a delete goes into this user's trash
  (Recycle Bin, Trash, freedesktop trash) or does not happen - there is no unlink.
* **`shell.run`** runs **only** in this device's own `make_sandbox()`: AppContainer on Windows,
  bwrap on Linux - with strong guarantees, the weaker mode is never accepted on a node. The
  node's folder is not mounted (Linux; the run is refused when it would lie under a read-only
  system bind) or not granted (Windows: owner-only ACL, no container grant) inside it. When there
  is no such sandbox (macOS, no bwrap, a weak backend), the call is refused with
  `sandbox_unavailable` - there is no unsandboxed fallback. A stop of the call (`nox/kill`, the
  dead-man rule) kills the sandboxed process tree. On macOS, which has no such sandbox at all,
  `shell.run` is not even offered (`hand_tools`), so no status claims it.
* **`package.*`** is not offered on nodes yet: a package manager needs rights the sandbox does
  not give, so it could not work there (it would take a narrow privileged helper, not built yet).

A refusal is raised as `NodeRefusal(reason)`; the agent answers it like a gate refusal and writes
it to the node's audit trail.

The package is cut along the three kinds of work above:

* `args` - the strict argument models of the file and shell tools and their refusal rules. The core
  builds its routed specs from the same models.
* `files` - the handlers of `file.*`, which open every path through `nox.node.device.safe_fs`.
* `node_hands`, `catalogue` - `NodeHands` (the shell, in the sandbox) and `hand_tools`, the tools a
  device offers.
"""
