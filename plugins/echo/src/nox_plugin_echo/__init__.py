"""Echo: the reference plugin for the plugin runtime (Plugin Architecture).

Registers the `echo.ping` tool, emits `echo.pong` and listens for `system.mode_changed`. It needs
no secrets and no network egress, so it is the smallest end-to-end proof that manifest -> worker ->
`plugin.register` -> tool call -> event emit works.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from nox.plugins.api import PluginApi
from nox.security.model import Risk


class PingInput(BaseModel):
    """Input of `echo.ping`."""

    text: str = Field(default="ping", max_length=500)


class EchoPlugin:
    def __init__(self, api: PluginApi) -> None:
        self.api = api
        self.last_mode = ""

    async def start(self) -> None:
        self.api.events.on("system.mode_changed", self._on_mode_changed)
        await self.api.events.emit("echo.pong", {"text": "ready"})

    async def stop(self) -> None:
        self.api.log.info("echo.stopped")

    async def _on_mode_changed(self, _name: str, payload: dict[str, Any]) -> None:
        self.last_mode = str(payload.get("current", ""))

    async def ping(self, data: PingInput) -> dict[str, Any]:
        await self.api.events.emit("echo.pong", {"text": data.text})
        return {"text": data.text, "greeting": str(self.api.config.get("greeting", "pong"))}


def create(api: PluginApi) -> EchoPlugin:
    plugin = EchoPlugin(api)
    api.tools.register(
        "echo.ping",
        PingInput,
        plugin.ping,
        Risk.READ,
        description="Echo the given text back and emit echo.pong.",
        side_effects=False,
        local=True,
    )
    return plugin
