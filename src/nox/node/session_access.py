"""Who may approve or change something while Nox controls a paired PC.

A computer-use session on a node (`nox.node.world_routes.NodeControl`) puts Nox's hands on that
PC's mouse and keyboard, so a click in a browser on that PC, or a message typed there, could be
Nox's own. `NodeSessionCondition` is the condition `nox.security.access.AccessPolicy` asks while
a session runs: it decides every "allow" and every request that changes something. Nox does not
know the PIN, so the PIN is the guard:

1. the browser on a node Nox controls never approves or changes anything - linked to it in the
   dashboard, or connecting from that node's address;
2. any other paired device, and this machine's dashboard or shell, only with the PIN (with the
   PIN's usual lockout; a paired device is removed after repeated wrong PINs);
3. this machine's dashboard or shell not from a remote session - a remote desktop could be
   opened from the very PC Nox controls; a core running as a service without a login session
   counts as not remote (the PIN still applies);
4. no PIN set: refused. Reading, "deny" and Stopp are never asked here;
5. a saturated address binding (nobody can be told apart from the controlled PC) refuses
   approvals and messages only;
6. `ESCAPES` - removing the node, lifting the lock, resuming - always work with the PIN, and
   removing from this machine's shell and dashboard without one.

Talking to Nox (`chat.send`, `voice.ptt`) needs no PIN - except from the browser on the PC Nox
controls, which could otherwise be the one typing; the remote-session check still holds for this
machine's own dashboard and shell. The decision tree is in `docs/ARCHITECTURE-NODES.md`
("Approvals during a node session").

This module owns that rule. The words of a refusal and the PIN checks are `nox.security.access`'s;
the session itself - what is locked, where the node runs - is `NodeControl`'s.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from nox.node.world_routes import NodeControl
from nox.security.access import (
    ALLOW,
    APPROVAL,
    REFUSE_LINKED,
    REFUSE_REMOTE,
    REFUSE_ROLE,
    REFUSE_SATURATED,
    Call,
    ClientContext,
    ClientKind,
    PinChecks,
    Verdict,
    conversation_request,
)

__all__ = ["ESCAPES", "NodeSessionCondition", "remote_now"]

#: How long the remote-session check may take (it may start `loginctl`); slower is "unknown".
REMOTE_CHECK_S = 3.0
#: Always possible with the PIN, whatever the session: removing a node (which needs no PIN from
#: this machine's own shell and dashboard), lifting a stuck lock, resuming after a stop. None of
#: them can be bound by a saturated binding or a node's addresses - they are how the owner gets
#: out of a lock a node holds.
ESCAPES: frozenset[str] = frozenset({"node.unpair", "node.computer.release", "security.resume"})
#: The one request an escape needs no PIN for, from this machine's own shell and dashboard:
#: removing narrows access.
_UNPAIR = "node.unpair"


async def remote_now(remote: Callable[[], bool | None]) -> bool | None:
    """`console_is_remote`, off the event loop and with a time limit (unknown when too slow)."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(remote), REMOTE_CHECK_S)
    except TimeoutError:
        return None


class NodeSessionCondition:
    """The access condition while Nox controls a node: see the module docstring."""

    name = "node_session"
    precedence = 20  # after the local input lock
    wire_reason = "node_session"
    reads_pin = True

    def __init__(
        self, control: NodeControl, pins: PinChecks, remote: Callable[[], bool | None]
    ) -> None:
        self._control = control
        self._pins = pins
        self._remote = remote

    async def check(self, client: ClientContext, call: Call) -> Verdict:
        control = self._control
        if not control.active():
            return ALLOW
        action = call.name
        if action in ESCAPES:
            return await self._escape(client, call)
        talking = conversation_request(action)
        binds = talking or action == APPROVAL
        if binds and control.is_saturated():
            # A node's binding is full: nobody is told apart from the controlled PC any more.
            return REFUSE_SATURATED
        if client.kind is ClientKind.DEVICE:
            if control.controls_browser(client.device_id) or control.from_controlled(
                client.peer, saturation=binds
            ):
                return REFUSE_LINKED
            if talking:
                return ALLOW
            return await self._pins.device_ui(client, call.pin, action)
        if client.kind is ClientKind.LOCAL_UI:
            return await self._local(client, call, talking=talking, binds=binds)
        return REFUSE_ROLE

    async def _local(
        self, client: ClientContext, call: Call, *, talking: bool, binds: bool
    ) -> Verdict:
        """This machine's dashboard or shell."""
        if talking:
            # Only the PIN is waived for talking, never the remote check (and talking on the
            # controlled machine itself counts as untrusted, `chat_untrusted`).
            return REFUSE_REMOTE if (await remote_now(self._remote)) is not False else ALLOW
        if self._control.controls_local(saturation=binds):
            # The controlled PC is this machine: its own dashboard and shell are that PC.
            return REFUSE_LINKED
        if await remote_now(self._remote) is not False:
            return REFUSE_REMOTE
        return await self._pins.local_ui(client, call.pin, call.name)

    async def _escape(self, client: ClientContext, call: Call) -> Verdict:
        """`ESCAPES` while a lock holds: the PIN, from any client that may send the request at
        all; `node.unpair` from this machine's own shell and dashboard without one."""
        if call.name == _UNPAIR and client.kind is ClientKind.LOCAL_UI:
            return ALLOW
        if client.kind is ClientKind.DEVICE:
            return await self._pins.device_ui(client, call.pin, call.name)
        return await self._pins.local_ui(client, call.pin, call.name)

    def chat_untrusted(self, client: ClientContext) -> bool:
        """Words that could have been typed by Nox on the PC it controls count as unreadable
        untrusted content: a paired device's, and this machine's own when the controlled PC is
        this machine."""
        if client.kind is ClientKind.DEVICE:
            return self._control.active()
        return client.kind is ClientKind.LOCAL_UI and self._control.controls_local()
