"""The core as the node extension sees it: narrow ports instead of an untyped `core`.

`install(core)` is handed the whole running core, which has a few dozen parts. The node extension
needs a handful of them, and only a few methods of each. Each `*Port` below is exactly that much,
as a `Protocol` the real part satisfies structurally - so a test hands in a small fake, and a
change to a part the hub does not use never touches this file.

`CoreServices.of(core)` is the one place that reads the core. It checks that every part the hub
needs is there and says which one is not, instead of letting a missing part show up later as an
`AttributeError` in the middle of the wiring; everything after it works on the typed bundle. The
single part that is truly optional - the network extension - is `None` in the bundle when it is not
installed, and the code that needs it asks.

Owns: the ports and the bundle. Does not own any wiring (`nox.node.hub.install`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from nox.core.config import NoxConfig
from nox.core.events import EventBus
from nox.data.db import Database
from nox.ipc.dispatch import RequestRegistry
from nox.ipc.overlay_tokens import OverlayTokenStore
from nox.lan.tls import TlsMaterial
from nox.node.wire.protocol import NodePairRequest
from nox.remote.models import DeviceRow
from nox.security.model import AuditLog, TemporaryGrant
from nox.security.privacy import CaptureKind
from nox.tools.registry import ToolRegistry

__all__ = [
    "CoreLike",
    "CoreServices",
    "HubPort",
    "KillSwitchPort",
    "LanPort",
    "NodePairHandler",
    "NodeWiringError",
    "OrchestratorPort",
    "PermissionEnginePort",
    "PrivacyPort",
    "SecurityPort",
    "TokensPort",
]


class NodeWiringError(RuntimeError):
    """The node extension cannot be installed on this core, and why (it says what to change)."""


class KillSwitchPort(Protocol):
    """The kill switch: asked whether it is engaged, engaged by a node, hooked for stop/resume."""

    def is_engaged(self) -> bool: ...

    async def engage(self, origin: str, reason: str = "") -> object: ...

    def register_stop_hook(
        self, name: str, hook: Callable[[], Awaitable[None]]
    ) -> Callable[[], None]: ...

    def register_resume_hook(
        self, name: str, hook: Callable[[], Awaitable[None]]
    ) -> Callable[[], None]: ...


class PrivacyPort(Protocol):
    """The privacy state: whether the panic button was pressed, whether a capture is allowed."""

    @property
    def panic(self) -> bool: ...

    def allows_capture(self, kind: CaptureKind) -> bool: ...


class PermissionEnginePort(Protocol):
    """The permission engine: remembered grants, tainted conversations, how a satellite is named."""

    def revoke_device_grants(self, device_id: str, *, reason: str, by: str = "system") -> int: ...

    def grant_temporary(self, grant: TemporaryGrant) -> None: ...

    def revoke(self, grant_id: str) -> None: ...

    def mark_tainted(self, conversation: str, content: str | None = None) -> None: ...

    def is_tainted(self, conversation: str) -> bool: ...

    def add_satellite_label(self, name: str, label: Callable[[str], str]) -> None: ...


class SecurityPort(Protocol):
    """The security services the hub uses."""

    @property
    def audit(self) -> AuditLog: ...

    @property
    def killswitch(self) -> KillSwitchPort: ...

    @property
    def privacy(self) -> PrivacyPort: ...

    @property
    def engine(self) -> PermissionEnginePort: ...


class OrchestratorPort(Protocol):
    """The assistant: where a voice satellite's words become a turn, and where the hub adds its
    line to the turn context."""

    def add_fact_source(self, name: str, source: Callable[[], str]) -> None: ...

    def conversation_for(self, channel: str) -> str: ...

    async def handle_text(
        self,
        text: str,
        *,
        language: str | None = None,
        speak: bool | None = None,
        on_chunk: Callable[[str], Awaitable[None] | None] | None = None,
        channel: str = ...,
        barge_in: bool = True,
        context_note: str = "",
    ) -> object: ...


class TokensPort(Protocol):
    """The token store: the overlay credentials, and who may hold one over the network."""

    @property
    def overlay(self) -> OverlayTokenStore | None: ...

    def add_node_overlay_check(self, name: str, check: Callable[[str], bool]) -> None: ...


class HubPort(Protocol):
    """The IPC hub: closing the connections of a credential that was revoked."""

    async def disconnect_credential(
        self, credential_ids: Iterable[str], reason: str = ...
    ) -> int: ...


#: Redeems a node's pairing request: the HTTP status and the JSON body of the answer.
NodePairHandler = Callable[[NodePairRequest], Awaitable[tuple[int, dict[str, Any]]]]


class PairedDevices(Protocol):
    """The browsers and phones paired over the network (`node.link` names one of them)."""

    def get(self, device_id: str) -> DeviceRow | None: ...


class LanPort(Protocol):
    """The network extension: its certificates, whether it serves, where nodes pair."""

    @property
    def tls(self) -> TlsMaterial | None: ...

    @property
    def running(self) -> bool: ...

    @property
    def port(self) -> int: ...

    @property
    def devices(self) -> PairedDevices: ...

    #: Where `POST /api/node/pair` goes; the hub sets it once.
    node_pair: NodePairHandler | None


class CoreLike(Protocol):
    """What the extension loader hands over: a `NoxCore` whose parts are built. Every part is
    `None` until the core builds it, which is why `CoreServices.of` looks before anything else."""

    @property
    def config(self) -> NoxConfig: ...

    @property
    def db(self) -> Database | None: ...

    @property
    def bus(self) -> EventBus | None: ...

    @property
    def security(self) -> SecurityPort | None: ...

    @property
    def registry(self) -> RequestRegistry | None: ...

    @property
    def tool_registry(self) -> ToolRegistry | None: ...

    @property
    def orchestrator(self) -> OrchestratorPort | None: ...

    @property
    def tokens(self) -> TokensPort | None: ...

    @property
    def hub(self) -> HubPort | None: ...


#: What a started core always has when extensions install (`NoxCore.start` builds them first).
REQUIRED_PARTS = (
    "config",
    "db",
    "bus",
    "security",
    "registry",
    "tool_registry",
    "orchestrator",
    "tokens",
    "hub",
)


@dataclass(frozen=True)
class CoreServices:
    """The parts of the core the node extension uses, checked and typed."""

    config: NoxConfig
    db: Database
    bus: EventBus
    security: SecurityPort
    registry: RequestRegistry
    tool_registry: ToolRegistry
    orchestrator: OrchestratorPort
    tokens: TokensPort
    hub: HubPort
    #: The network extension, when it is installed (`ipc.lan`); nodes pair and connect over it.
    lan: LanPort | None

    @classmethod
    def of(cls, core: CoreLike) -> CoreServices:
        """The services of `core`; raises `NodeWiringError` naming every part that is missing."""
        missing = [name for name in REQUIRED_PARTS if getattr(core, name, None) is None]
        if missing:
            raise NodeWiringError(
                "the node extension needs "
                + ", ".join(f"core.{name}" for name in missing)
                + ", which this core has not built: install it after the core is started"
            )
        assert core.db is not None and core.bus is not None and core.security is not None
        assert core.registry is not None and core.tool_registry is not None
        assert core.orchestrator is not None and core.tokens is not None and core.hub is not None
        lan: LanPort | None = getattr(core, "lan_runtime", None)  # set by the `lan` extension
        return cls(
            config=core.config,
            db=core.db,
            bus=core.bus,
            security=core.security,
            registry=core.registry,
            tool_registry=core.tool_registry,
            orchestrator=core.orchestrator,
            tokens=core.tokens,
            hub=core.hub,
            lan=lan,
        )
