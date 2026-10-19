"""`McpGate`: the core side of the MCP bridge - the only door from Claude Code to Nox's tools.

Per Claude Code process the gate prepares one bridge: a client id, a one-time token bound to the
IPC role `mcp` (`TokenStore.issue_worker_token(..., role="mcp")`), and an owner-only MCP config
file that starts `python -m nox.mcp.bridge` with them. The bridge authenticates once; the token is
spent with that, and the config file is deleted as soon as the bridge is connected.

Two requests exist for the role, and nothing else:

* `mcp.tools.list` - the offer as it stands now, in wire names. Listing is harmless; it carries no
  turn token because Claude Code lists tools when it starts, before any turn.
* `mcp.tools.call` - one call. It must carry the token of an open turn (`ToolLoop.begin`), which the
  core pushes to the bridge right before the user's message goes to Claude Code and which dies when
  the turn ends. A call without it, with an old one, from another client, or for a tool outside the
  turn's offer is refused; an admitted call runs through `ToolLoop.execute` -> `ToolExecutor` ->
  `PermissionEngine` -> audit like any other.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from nox.ai.providers.claude_persistent import Launch
from nox.ai.tooluse import render_result
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.core.toolloop import ToolLoop
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.tokens import WORKER_TOKEN_ENV, TokenStore, write_secret_file
from nox.mcp.protocol import CLIENT_ID_ENV, HUB_URL_ENV, MCP_ROLE
from nox.tools.names import from_wire, to_wire

log = get_logger(__name__)

__all__ = ["McpGate", "McpLaunch", "ToolsCallPayload"]

SERVER_KEY = "nox"
#: The one-time token is spent when the bridge connects, which `ClaudeSession.initialize` makes
#: happen within seconds of the start; the margin covers a slow login.
BRIDGE_TOKEN_TTL_S = 120.0
TURN_PUSH_TIMEOUT_S = 3.0
#: Longest description the model reads per tool, cut on a word. Native tool definitions are
#: cached by the model provider with the rest of the prefix, so this is about clarity, not cost.
DESCRIPTION_LIMIT = 300


def _bounded(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= DESCRIPTION_LIMIT:
        return flat
    return flat[:DESCRIPTION_LIMIT].rsplit(" ", 1)[0] + "..."


class ToolsCallPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=16, max_length=128)
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class Hub(Protocol):
    """The subset of `IpcHub` the gate uses (structural, so tests can pass a fake)."""

    async def request(
        self,
        client_id: str,
        name: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]: ...

    async def disconnect(self, client_id: str, reason: str = ...) -> None: ...


@dataclass(frozen=True)
class McpLaunch:
    """One prepared bridge: the config file to hand Claude Code and the client id it will use."""

    client_id: str
    config_path: Path


class McpGate:
    def __init__(
        self,
        *,
        hub: Hub,
        hub_url: Callable[[], str],
        tokens: TokenStore,
        bus: EventBus,
        tool_loop: Callable[[], ToolLoop | None],
        runtime_dir: Path,
        command: Sequence[str],
        cloud_allowed: Callable[[], bool] | None = None,
        on_bridge_lost: Callable[[str], None] | None = None,
    ) -> None:
        """`cloud_allowed` is asked before every call: Claude Code is a cloud model, and a
        switch to privacy mode private or offline must stop it mid-turn, not at the next turn.
        `on_bridge_lost` hears when a bridge that was not retired drops out."""
        self._cloud_allowed = cloud_allowed
        self._on_bridge_lost = on_bridge_lost
        self._hub = hub
        self._hub_url = hub_url
        self._tokens = tokens
        self._bus = bus
        self._tool_loop = tool_loop
        self._dir = runtime_dir / "mcp"
        self._command = list(command)
        self._launches: dict[str, McpLaunch] = {}
        self._connected: dict[str, asyncio.Event] = {}
        #: The latest turn token per bridge, kept until it is delivered or the bridge retires.
        self._pending: dict[str, str] = {}
        self._tasks: set[asyncio.Task[bool]] = set()
        self._unsubs: list[Callable[[], None]] = []

    # -- wiring ------------------------------------------------------------------------------------

    def register(self, registry: RequestRegistry) -> None:
        registry.register("mcp.tools.list", EmptyPayload, self._list, roles=(MCP_ROLE,))
        registry.register("mcp.tools.call", ToolsCallPayload, self._call, roles=(MCP_ROLE,))
        self._unsubs = [
            self._bus.subscribe(E.IPC_CLIENT_CONNECTED, self._on_connected),
            self._bus.subscribe(E.IPC_CLIENT_DISCONNECTED, self._on_disconnected),
        ]

    async def close(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        for launch in list(self._launches.values()):
            await self.retire(launch)

    # -- bridge lifecycle --------------------------------------------------------------------------

    def prepare(self) -> McpLaunch:
        """A config file for one Claude Code process, with a fresh one-time bridge token."""
        client_id = f"mcp:{uuid.uuid4().hex[:12]}"
        token = self._tokens.issue_worker_token(client_id, ttl_s=BRIDGE_TOKEN_TTL_S, role=MCP_ROLE)
        config = {
            "mcpServers": {
                SERVER_KEY: {
                    "command": self._command[0],
                    "args": [*self._command[1:], "-m", "nox.mcp.bridge"],
                    "env": {
                        HUB_URL_ENV: self._hub_url(),
                        WORKER_TOKEN_ENV: token,
                        CLIENT_ID_ENV: client_id,
                    },
                }
            }
        }
        path = self._dir / f"{client_id.replace(':', '-')}.json"
        restricted = write_secret_file(path, json.dumps(config))
        if not restricted:
            log.warning("mcp.config_acl_not_restricted", client=client_id)
        launch = McpLaunch(client_id=client_id, config_path=path)
        self._launches[client_id] = launch
        self._connected[client_id] = asyncio.Event()
        return launch

    def _on_connected(self, event: Event) -> None:
        client_id = str(event.payload.get("client_id", ""))
        connected = self._connected.get(client_id)
        if connected is None or event.payload.get("role") != MCP_ROLE:
            return
        connected.set()
        launch = self._launches.get(client_id)
        if launch is not None:
            # The token in it is spent; nothing in the file is useful to anyone any more.
            launch.config_path.unlink(missing_ok=True)
        if client_id in self._pending:
            task = asyncio.ensure_future(self._push(client_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def _on_disconnected(self, event: Event) -> None:
        client_id = str(event.payload.get("client_id", ""))
        # A retired bridge is no longer in `_launches`; only an unexpected loss is reported.
        if client_id in self._launches and self._on_bridge_lost is not None:
            log.warning("mcp.bridge_lost", client=client_id)
            self._on_bridge_lost(client_id)

    async def activate(self, launch: Launch, token: str) -> bool:
        """Hand the bridge of `launch` the token of the turn that is about to start.

        Claude Code starts its MCP servers only once the first message arrives, so a fresh bridge
        may not be connected yet: the token is then kept and pushed the moment it connects, which
        is long before the model can get to its first tool call. False = not delivered (yet).
        """
        loop = self._tool_loop()
        turn = loop.by_token(token) if loop is not None else None
        if turn is not None:
            # The token works only for the bridge it was handed to.
            turn.client = launch.client_id
        self._pending[launch.client_id] = token
        connected = self._connected.get(launch.client_id)
        if connected is None or not connected.is_set():
            return False
        return await self._push(launch.client_id)

    async def _push(self, client_id: str) -> bool:
        token = self._pending.get(client_id)
        if not token:
            return False
        try:
            await self._hub.request(
                client_id, "mcp.turn", {"token": token}, timeout=TURN_PUSH_TIMEOUT_S
            )
        except (TimeoutError, IpcError) as exc:
            log.warning("mcp.token_push_failed", client=client_id, error=f"{type(exc).__name__}")
            return False
        return True

    async def retire(self, launch: Launch) -> None:
        """The Claude Code process of `launch` is gone or going: cut its bridge off now."""
        self._launches.pop(launch.client_id, None)
        self._connected.pop(launch.client_id, None)
        self._pending.pop(launch.client_id, None)
        launch.config_path.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            await self._hub.disconnect(launch.client_id, "claude code session ended")

    # -- requests ----------------------------------------------------------------------------------

    def _bridge(self, ctx: RequestContext) -> None:
        if ctx.role != MCP_ROLE or ctx.client_id not in self._launches:
            raise IpcError(ERR_PERMISSION, "not a bridge this core started")

    async def _list(self, ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        self._bridge(ctx)
        loop = self._tool_loop()
        offer = loop.current_offer() if loop is not None else []
        tools = []
        for tool in offer:
            description = _bounded(tool.description)
            if tool.asks_first:
                description += " (Asks the user for confirmation first.)"
            tools.append(
                {
                    "name": to_wire(tool.name),
                    "description": description,
                    "inputSchema": tool.schema_ or {"type": "object", "properties": {}},
                }
            )
        return {"tools": tools}

    async def _call(self, ctx: RequestContext, payload: ToolsCallPayload) -> dict[str, Any]:
        self._bridge(ctx)
        loop = self._tool_loop()
        turn = loop.by_token(payload.token) if loop is not None else None
        if loop is None or turn is None:
            log.info("mcp.call_without_open_turn", client=ctx.client_id)
            raise IpcError(ERR_PERMISSION, "no open turn for this token")
        if turn.client != ctx.client_id:
            log.warning("mcp.call_from_another_bridge", client=ctx.client_id)
            raise IpcError(ERR_PERMISSION, "this turn belongs to another bridge")
        if self._cloud_allowed is not None and not self._cloud_allowed():
            log.info("mcp.call_refused_privacy", client=ctx.client_id)
            raise IpcError(
                ERR_PERMISSION, "the privacy mode does not allow a cloud model to use tools now"
            )
        name = from_wire(payload.name)
        run = await loop.execute(turn, name, payload.arguments)
        return {
            "ok": run.ok,
            "text": render_result(name, ok=run.ok, data=run.data, error=run.error),
        }
