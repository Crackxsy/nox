"""`install(core)`: wires the smart-home IPC surface onto a started core.

The feature itself lives in the `home` plugin worker; the core only needs the requests the
dashboard calls and the connection probe the Settings page uses. Both are registered
unconditionally, so a fresh installation gets "the home plugin is not running, here is how to turn
it on" instead of `unknown request` and a page that cannot explain itself.

Returns `None` - there is nothing to shut down here. The plugin worker is owned by the
`PluginManager`, and it stops the same way every other plugin does.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

import httpx

from nox.core.config import NoxConfig
from nox.core.extension import ExtensionRuntime
from nox.core.logging import get_logger
from nox.home.ipc import register_home_ipc
from nox.ipc.dispatch import RequestRegistry
from nox.tools.executor import ToolExecutor

log = get_logger(__name__)

ACCESS_TOKEN_SECRET = "nox/home/access_token"  # noqa: S105 - a secret *name*, not a value


class _SecurityLike(Protocol):
    secrets: Any
    egress: Any


class CoreLike(Protocol):
    """The subset of `NoxCore` this module needs (structural, so a test needs no real core)."""

    config: NoxConfig
    tool_executor: ToolExecutor
    registry: RequestRegistry
    security: _SecurityLike
    state: Any


class _StateLike(Protocol):
    def get(self, path: str) -> object: ...


def _mode(core: CoreLike) -> str:
    """Current assistant mode, defaulting to `companion` so a tool call is never left modeless."""
    state: _StateLike = core.state
    try:
        value = state.get("assistant.mode")
    except Exception as exc:  # noqa: BLE001 - never let a mode lookup break a tool call
        log.warning("home.mode_lookup_failed", error=f"{type(exc).__name__}: {exc}")
        return "companion"
    return str(value) if value else "companion"


def install(core: CoreLike) -> ExtensionRuntime:
    def read_token() -> str | None:
        try:
            value = core.security.secrets.get(ACCESS_TOKEN_SECRET)
        except Exception as exc:  # noqa: BLE001 - an unavailable keyring is "no token"
            log.warning("home.token_read_failed", error=type(exc).__name__)
            return None
        return None if value is None else str(value)

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        """Every probe goes through the core's egress guard, like every other outbound request."""
        return cast(httpx.AsyncClient, core.security.egress.client(**kwargs))

    register_home_ipc(
        core.registry,
        core.tool_executor,
        lambda: _mode(core),
        settings=lambda: core.config.home,
        token=read_token,
        client_factory=client_factory,
    )
    log.info("home.installed", host=core.config.home.host, port=core.config.home.port)
    return None
