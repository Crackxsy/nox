"""`nox node run`: the node's connection to its core, kept up for as long as the node runs.

The node dials out (`wss://<core>:<node_port>/node/v1`), with its client certificate, trusting
only the authority it pinned at pairing; the core never connects to a node. It introduces itself
with `initialize`, then serves the core as an MCP server: `tools/list` lists what `NodeGate`
offers, `tools/call` runs only what `NodeGate` allows. Everything the core sends outside
`CORE_TO_NODE` is refused.

Connection lost: reconnect with backoff, 1 s doubling to 60 s, and never act on its own meanwhile.
`nox/revoked`: the node deletes its key and certificate and stops ("Von Nox entfernt").
The node pings every second; the core's pings are answered.

Stopping (see docs/ARCHITECTURE-NODES.md, "Stopping"):
* `nox/kill` from the core: every running call is cancelled at once, the node answers
  `nox/killed` with their ids and refuses everything until the core's `nox/resume`.
* Dead man: while a call with a side effect runs and nothing came from the core for
  `DEAD_MAN_S`, that call is aborted and the node refuses everything until the core is back and
  not stopped.
* Stopped on this device (`nox node stop`): the same, plus `security.kill` to the core - which
  stops Nox everywhere. Only this device resumes it (`nox node resume`).

`NodeAgent` is one class built in layers, each in its own module and each using only what the layers
below it provide, so no module imports one above it:

* `base.AgentBase` - the agent's state (set once in `__init__`), the stop state and the small
  helpers every part uses: replies and audit lines.
* `calls.CallRunner` - running one call: the gate, the question on the device, the dead-man rule,
  the blob chunks, and cancelling calls (kill, local stop, lost connection).
* `core_requests.CoreRequests` - what the node asks or tells the core outside a tool call, and the
  renewal of its certificate.
* `node_agent.NodeAgent` - the connection (`run`, backoff), one session and the dispatch of what the
  core sends.

`dialog_text` (the sentences of the question on the device) and `netinfo` (the addresses the node
reports) stand apart from the class.
"""
