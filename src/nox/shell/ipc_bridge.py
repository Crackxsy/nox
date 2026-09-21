"""Adapter between the shell and `nox.ipc.client.IpcClientThread` (IPC Model, role `shell`).

The shell depends only on the `IpcBridge` protocol so tests can inject a fake and so the shell
still starts when the real client module is missing or the core is down (Process Model: "If the
core is down, the shell shows a degraded pet state and the tray still reaches the supervisor").
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, Protocol

EventCallback = Callable[[dict[str, Any]], None]


class IpcBridge(Protocol):
    def start(self) -> None: ...
    def call(self, name: str, payload: dict[str, Any]) -> Future[Any]: ...
    def on_event(self, callback: EventCallback) -> None: ...
    def stop(self) -> None: ...


BridgeFactory = Callable[[str, str, str, str], IpcBridge]


class BridgeUnavailableError(RuntimeError):
    """The real IPC client cannot be created (module missing or constructor failed)."""


def create_bridge(url: str, token: str, role: str, client_id: str) -> IpcBridge:
    """Instantiate the real client. Raises BridgeUnavailableError instead of crashing the shell."""
    try:
        from nox.ipc.client import IpcClientThread
    except ImportError as exc:
        raise BridgeUnavailableError("nox.ipc.client.IpcClientThread not available") from exc
    try:
        bridge: IpcBridge = IpcClientThread(url, token, role, client_id)
    except Exception as exc:  # constructor contract is owned by the IPC agent
        raise BridgeUnavailableError(f"IpcClientThread failed: {type(exc).__name__}") from exc
    return bridge
