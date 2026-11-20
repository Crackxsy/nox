"""Who may do what while Nox's own hands are on the machine - decided in one place.

Two situations make a click or a keystroke suspect: synthetic input on this machine (computer use
- `InputLock`) and a computer-use session on a paired PC (`nox.node.hub.session_access`). While
either holds, a button in the dashboard could have been pressed by Nox itself, so a request that
changes something - and an "allow" for a permission prompt - has to come from somebody Nox cannot
be.

`AccessPolicy.decide` answers for a request, `AccessPolicy.approval` for a prompt's answer, both
from the same inputs: *who* (`ClientContext`, built once per connection), *what* (the request's
name and payload) and the *conditions* that are installed. Each condition is a provider in a list,
not a slot: a third one is one `add_condition` call and cannot overwrite the others. The first
condition that does not allow decides; they speak in the order of their `precedence`.

The answer is a `Verdict`: `Allow`, `Refuse` (cannot be done by this client, now) or `NeedsPin`
(can be done with the security PIN, and it was missing or wrong). A surface turns a verdict into
its own wire shape - an IPC error, a `{"ok": false}` answer - so the rule itself knows nothing of
either. The security PIN's own checks (this machine's gate, a paired device's guard with its
lockout) live in `PinChecks`, so the conditions share them instead of each carrying a copy.

This module owns the rule and the words of every refusal (`REFUSE_*`, with a stable `code` the
dashboard words itself). It knows no role names, no node, no transport: what a role *is* is
decided where connections are authenticated (`nox.ipc.dispatch.client_context`), and what a node
session *means* in `nox.node.hub.session_access`. Principle P1 (the kill switch and the permission
engine are enforced in code, below the model) is untouched: this decides who may click, not
what Nox may do.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from nox.security._logging import get_logger
from nox.security.gate import PinRequiredError
from nox.security.model import Risk

log = get_logger(__name__)

__all__ = [
    "ALLOW",
    "APPROVAL",
    "CALM_UI_REQUESTS",
    "CONVERSATION_UI_REQUESTS",
    "REFUSE_LINKED",
    "REFUSE_NO_PIN",
    "REFUSE_REMOTE",
    "REFUSE_ROLE",
    "REFUSE_SATURATED",
    "SUB_LINKED",
    "SUB_NO_PIN",
    "SUB_PIN",
    "SUB_REMOTE",
    "SUB_ROLE",
    "SUB_SATURATED",
    "AccessPolicy",
    "Allow",
    "Approval",
    "Call",
    "ClientContext",
    "ClientKind",
    "Condition",
    "InputLock",
    "NeedsPin",
    "PeerClass",
    "PinChecks",
    "Refuse",
    "Verdict",
    "calm_call",
    "calm_ui_request",
    "conversation_request",
    "peer_class_of",
]

#: The request name an answer to a permission prompt travels under, and what a condition sees as
#: `Call.name` when it is asked about one.
APPROVAL = "security.permission.reply"


# ---- which requests are calm ---------------------------------------------------------------------

#: What a UI client may still do while Nox controls mouse and keyboard: look, stop, mute.
CALM_UI_REQUESTS: frozenset[str] = frozenset(
    {"state.get", "health.get", "health.history", "config.effective", "config.get",
     "security.kill", "security.panic", "security.pin.status", "voice.mute", "plugin.status",
     "secrets.status", "security.permission.reply", "security.permission.pending",
     "personality.get", "day.today"}
)  # fmt: skip
#: Talking to Nox: while Nox controls mouse and keyboard on a paired PC these need no PIN - from
#: anywhere but the browser on that very PC, so Nox cannot type instructions to itself
#: (`nox.node.hub.session_access`). Anything they lead to is asked as usual.
CONVERSATION_UI_REQUESTS: frozenset[str] = frozenset({"chat.send", "voice.ptt"})


def calm_ui_request(name: str) -> bool:
    """A request that only reads or stops - allowed while synthetic input is in play."""
    return (
        name in CALM_UI_REQUESTS or name.startswith("ipc.") or name.endswith((".list", ".status"))
    )


def conversation_request(name: str) -> bool:
    """Talking to Nox (`CONVERSATION_UI_REQUESTS`)."""
    return name in CONVERSATION_UI_REQUESTS


def calm_call(name: str, payload: Any) -> bool:
    """`calm_ui_request` for one call. `voice.mute` is calm only when it mutes - `muted` literally
    `true`; anything else (unmuting, "false", 0, a missing field) is not: it could give Nox an ear
    again. The field is a strict boolean, so the model never reads it otherwise. Every lock uses
    this one rule, so the same request is never calm under one lock and not under another."""
    if name == "voice.mute":
        return isinstance(payload, dict) and payload.get("muted") is True
    return calm_ui_request(name)


# ---- who is asking -------------------------------------------------------------------------------


class ClientKind(StrEnum):
    """What a client is to the rule, whatever its role is called."""

    #: This machine's own desktop shell or dashboard: the person at the keyboard (or Nox).
    LOCAL_UI = "local_ui"
    #: A paired device: a browser that may run on the very PC Nox steers.
    DEVICE = "device"
    #: Everything else (a worker, a plugin, the overlay): no lock applies, no approval is theirs.
    OTHER = "other"


class PeerClass(StrEnum):
    """Where a connection comes from. Diagnostic only: it goes into the log line of a refusal and
    takes no part in any decision - what counts as the controlled PC is its address
    (`NodeControl.from_controlled`), not its network."""

    LOCAL = "local"
    LAN = "lan"
    TAILNET = "tailnet"


#: Tailscale's carrier-grade NAT range and its IPv6 prefix.
_TAILNET_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


def peer_class_of(peer: str) -> PeerClass:
    """`peer` is the address a paired device connects from ("" for a client on this machine).
    An address that cannot be read counts as the home network: not this machine."""
    if not peer:
        return PeerClass.LOCAL
    try:
        address = ipaddress.ip_address(peer.strip("[]"))
    except ValueError:
        return PeerClass.LAN
    if address.is_loopback:
        return PeerClass.LOCAL
    if any(
        address in network for network in _TAILNET_NETWORKS if address.version == network.version
    ):
        return PeerClass.TAILNET
    return PeerClass.LAN


@dataclass(frozen=True, slots=True)
class ClientContext:
    """Who is asking, built once per connection (`nox.ipc.dispatch.client_context`)."""

    client_id: str
    role: str
    kind: ClientKind
    #: The paired device behind a `DEVICE` client; "" for every other kind.
    device_id: str = ""
    #: The address a device connects from ("" for a client on this machine).
    peer: str = ""
    peer_class: PeerClass = PeerClass.LOCAL

    @property
    def is_person_ui(self) -> bool:
        """A role a person clicks in - and so one synthetic input could click in for them."""
        return self.kind is not ClientKind.OTHER


@dataclass(frozen=True, slots=True)
class Call:
    """What is asked: a request's name (`APPROVAL` for the answer to a permission prompt) and the
    PIN it carried, if any."""

    name: str
    pin: str | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    """An answer to a permission prompt."""

    allow: bool
    pin: str | None
    #: The risk of the prompt that is being answered; read only when it matters (a paired
    #: device's "allow"), so a lookup the rule does not need is never made.
    risk: Callable[[], Risk | None]


# ---- the answer ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Allow:
    """Nothing in the way."""

    #: A condition that reads the request's `pin` field ran: the PIN was for the check only, so
    #: a request that takes none does not see it.
    pin_read: bool = False


@dataclass(frozen=True, slots=True)
class Refuse:
    """Not possible for this client now, whatever it adds."""

    #: An English sentence, the fallback for logs, the audit and clients without the code.
    reason: str
    #: A stable code the dashboard words itself (`details.sub_reason`); "" when there is none.
    code: str = ""
    #: What kind of condition refused (`Condition.wire_reason`); stamped by the policy.
    category: str = ""

    def wire_details(self) -> dict[str, str] | None:
        """The details of the IPC error this becomes, or None for a plain message."""
        if not self.category:
            return None
        details = {"reason": self.category}
        if self.code:
            details["sub_reason"] = self.code
        return details


@dataclass(frozen=True, slots=True)
class NeedsPin:
    """Possible with the security PIN: it was missing or wrong. Its `reason` is the gate's own."""

    reason: str
    code: str = ""
    category: str = ""

    def wire_details(self) -> dict[str, str] | None:
        return Refuse(self.reason, self.code, self.category).wire_details()


Verdict = Allow | Refuse | NeedsPin
ALLOW = Allow()

SUB_LINKED = "node_session_linked"
SUB_REMOTE = "node_session_remote"
SUB_NO_PIN = "node_session_no_pin"
SUB_SATURATED = "node_session_saturated"
SUB_ROLE = "node_session_role"
SUB_PIN = "node_session_pin"

REFUSE_LINKED = Refuse(
    "Nox controls mouse and keyboard on the PC this runs on; nothing can be approved or changed "
    "from here until that ends - deny or stop",
    SUB_LINKED,
)
REFUSE_REMOTE = Refuse(
    "Nox controls mouse and keyboard on a paired PC and this computer is used remotely (or that "
    "cannot be told); approve at the computer itself with the PIN, or deny or stop",
    SUB_REMOTE,
)
REFUSE_NO_PIN = Refuse(
    "Nox controls mouse and keyboard on a paired PC; approving or changing anything now needs "
    "the security PIN, and none is set - deny, stop, or set a PIN first",
    SUB_NO_PIN,
)
REFUSE_SATURATED = Refuse(
    "Nox controls a device with very many addresses; approvals and messages wait until the "
    "session ends - deny and stop always work",
    SUB_SATURATED,
)
REFUSE_ROLE = Refuse(
    "Nox controls mouse and keyboard on a paired PC; this client cannot approve now", SUB_ROLE
)

LOCK_REQUEST = Refuse(
    "Nox is controlling mouse and keyboard; end it with the stop hotkey and try again"
)
LOCK_APPROVAL = Refuse("Nox controls mouse and keyboard; press the stop hotkey, then allow")


# ---- the PIN -------------------------------------------------------------------------------------


class PinGateLike(Protocol):
    """The core's security PIN (`nox.security.gate.SecurityChangeGate`)."""

    def is_required(self) -> bool: ...

    async def require(self, pin: str | None, *, action: str, by: str) -> None: ...


class DevicePinLike(Protocol):
    """A paired device's PIN door (`nox.security.device_pin.DevicePinGuard`): raises an
    `IpcError` of its own - with the reasons `pin_required`, `pin_wrong`, `removed` - unless the
    device gave the right PIN, and removes a device after repeated wrong ones."""

    async def require(self, client: ClientContext, pin: str | None, action: str) -> None: ...


def _no_gate() -> PinGateLike | None:
    return None


@dataclass
class PinChecks:
    """The security PIN, for a client of each kind. Wired where the gate and the device guard
    are built (`nox.app`, `nox.lan.install`); until then no PIN is set, which refuses."""

    gate: Callable[[], PinGateLike | None] = _no_gate
    device_guard: DevicePinLike | None = field(default=None)

    async def local_ui(self, client: ClientContext, pin: str | None, action: str) -> Verdict:
        """This machine's own shell or dashboard with the security PIN - from a remote session too
        (an SSH login to a headless core), because only the owner knows the PIN."""
        if client.kind is not ClientKind.LOCAL_UI:
            return REFUSE_ROLE
        gate = self.gate()
        if gate is None or not gate.is_required():
            return REFUSE_NO_PIN
        try:
            await gate.require(pin, action=action, by=client.client_id or client.role)
        except PinRequiredError as exc:
            return NeedsPin(exc.reason, SUB_PIN)
        return ALLOW

    async def device_ui(self, client: ClientContext, pin: str | None, action: str) -> Verdict:
        """A paired device with the security PIN (its own lockout applies, `DevicePinGuard`)."""
        from nox.ipc.errors import IpcError  # noqa: PLC0415 - nox.ipc imports this module

        gate = self.gate()
        if gate is None or not gate.is_required():
            return REFUSE_NO_PIN
        if self.device_guard is None:
            return REFUSE_ROLE
        try:
            await self.device_guard.require(client, pin, action)
        except IpcError as exc:
            return NeedsPin(exc.message, SUB_PIN)
        return ALLOW

    async def admin(
        self, client: ClientContext, pin: str | None, action: str, *, local_needs_pin: bool
    ) -> Verdict:
        """Administration - pairing, removing, narrowing. From a paired device: the PIN, always
        (`DevicePinGuard`, which raises its own error and removes a device after repeated wrong
        PINs). Locally: the PIN gate as configured where `local_needs_pin`, nothing otherwise -
        removing a device only ever narrows access, and a thief must not be able to block it by
        locking the PIN."""
        if client.kind is ClientKind.DEVICE:
            if self.device_guard is None:
                return Refuse(f"{action} is not possible from a paired device")
            await self.device_guard.require(client, pin, action)
            return ALLOW
        if not local_needs_pin:
            return ALLOW
        gate = self.gate()
        if gate is None:
            return Refuse(f"{action} needs the security PIN, which is not available")
        try:
            await gate.require(pin, action=action, by=client.client_id or client.role)
        except PinRequiredError as exc:
            return NeedsPin(exc.reason)
        return ALLOW


# ---- conditions ----------------------------------------------------------------------------------


class Condition(Protocol):
    """One reason a UI client's request may have to wait: a provider the policy asks in turn."""

    #: Unique among the installed conditions.
    name: str
    #: Who speaks first when several refuse: the lower number. A fixed rank, not the order the
    #: extensions happen to install in - the words of a refusal must not depend on boot order.
    precedence: int
    #: The `details.reason` of the IPC error this condition's refusals become; "" for a plain
    #: message without details.
    wire_reason: str
    #: Whether `check` reads the PIN carried in the request (see `Allow.pin_read`).
    reads_pin: bool

    async def check(self, client: ClientContext, call: Call) -> Verdict:
        """Allow, or say why not. Called only for a call that is not calm (`calm_call`) from a
        client a person clicks in; an `APPROVAL` is asked of every client."""
        ...

    def chat_untrusted(self, client: ClientContext) -> bool:
        """Whether words from this client may have been typed by Nox itself."""
        ...


class InputLock:
    """Synthetic input could be in play on this machine (`nox.desktop.computer_use`): a UI client
    keeps only what is calm, and an "allow" does not count - no click Nox sends can activate,
    approve or change anything. The stop hotkey, which Nox never sends, ends it."""

    name = "input_lock"
    precedence = 10
    wire_reason = ""
    reads_pin = False

    def __init__(self, active: Callable[[], bool]) -> None:
        self._active = active

    async def check(self, client: ClientContext, call: Call) -> Verdict:
        if not self._active():
            return ALLOW
        return LOCK_APPROVAL if call.name == APPROVAL else LOCK_REQUEST

    def chat_untrusted(self, client: ClientContext) -> bool:
        return False


# ---- the policy ----------------------------------------------------------------------------------


class AccessPolicy:
    """The one place that decides who may do what while synthetic input or a node session is in
    play. `RequestRegistry.dispatch` asks `decide`; the permission prompt asks `approval`."""

    def __init__(self, *, gate: Callable[[], PinGateLike | None] = _no_gate) -> None:
        self._conditions: list[Condition] = []
        self.pins = PinChecks(gate=gate)

    def add_condition(self, condition: Condition) -> None:
        """Install a condition; conditions are asked in the order of their `precedence`."""
        if any(c.name == condition.name for c in self._conditions):
            raise ValueError(f"access condition {condition.name!r} is already installed")
        self._conditions.append(condition)
        self._conditions.sort(key=lambda c: c.precedence)  # stable

    def condition_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self._conditions)

    async def decide(self, client: ClientContext, name: str, payload: Any) -> Verdict:
        """A request from `client`. Only a client a person clicks in is held to a condition, and
        only for a request that is not calm."""
        if not client.is_person_ui or calm_call(name, payload):
            return ALLOW
        raw = payload.get("pin") if isinstance(payload, dict) else None
        return await self._ask(client, Call(name, raw if isinstance(raw, str) else None))

    async def approval(self, client: ClientContext, answer: Approval) -> Verdict:
        """An answer to a permission prompt. A "deny" always counts. An "allow" must pass every
        condition, and from a paired device at high or critical risk it needs the PIN as well -
        otherwise a phone could ask Nox for something through the chat and then allow it itself.

        A device that fails its PIN door - or has none to knock on - gets an `IpcError`, not a
        verdict: the door's wire shape and its removal of a device after repeated wrong PINs are
        that guard's contract."""
        if not answer.allow:
            return ALLOW
        verdict = await self._ask(client, Call(APPROVAL, answer.pin))
        if not isinstance(verdict, Allow):
            return verdict
        if client.kind is ClientKind.DEVICE and answer.risk() in (Risk.HIGH, Risk.CRITICAL):
            guard = self.pins.device_guard
            if guard is None:
                from nox.ipc.errors import ERR_PERMISSION, IpcError  # noqa: PLC0415

                raise IpcError(ERR_PERMISSION, "a paired device cannot allow this")
            await guard.require(client, answer.pin, APPROVAL)
        return verdict

    def chat_untrusted(self, client: ClientContext) -> bool:
        """Whether a message from `client` may have been typed by Nox: the conversation then holds
        unreadable untrusted content, so no lookup in it counts as the user's own and every
        off-machine call asks."""
        return any(condition.chat_untrusted(client) for condition in self._conditions)

    async def _ask(self, client: ClientContext, call: Call) -> Verdict:
        pin_read = False
        for condition in self._conditions:
            verdict = await condition.check(client, call)
            pin_read = pin_read or condition.reads_pin
            if isinstance(verdict, Allow):
                continue
            log.debug(
                "access.refused",
                condition=condition.name,
                request=call.name,
                client=client.client_id,
                peer_class=client.peer_class.value,
            )
            return replace(verdict, category=condition.wire_reason)
        return Allow(pin_read=pin_read)
