"""The `ai.setup.*` requests behind the dashboard's "Lokale KI" tile.

    ai.setup.status  {}                       -> the last run's state (`SetupState.to_payload`)
    ai.setup.check   {}                       -> measure and recommend now (a dry run), then state
    ai.setup.start   {install_ollama?: bool}  -> {ok, reason?, ...state}; the run continues in the
                                                 background and the page polls `status`

Clicking "Einrichten" is the consent to the download, so a dashboard run answers the download
question with yes. Installing Ollama needs `install_ollama: true` on top, and even then it goes
through `package.install` and the `ToolExecutor`, where the permission engine asks once more.
Only one run at a time; a second `start` while one is running is refused, not queued.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from nox.core.logging import get_logger
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.roles import LOCAL_UI_ROLES
from nox.localai.setup import Question, SetupDeps, SetupState, run_setup

log = get_logger(__name__)

__all__ = ["LocalAiService", "SetupStart", "register_localai_ipc"]

#: The two user-facing clients, never a plugin, a worker or a phone.


class SetupStart(BaseModel):
    install_ollama: bool = False


class LocalAiService:
    """Holds the one run the dashboard can see, and starts new ones."""

    def __init__(self, deps: Callable[[], SetupDeps]) -> None:
        self._deps = deps
        self.state = SetupState()
        self._task: asyncio.Task[SetupState] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return self.state.to_payload()

    async def check(self) -> dict[str, Any]:
        """A dry run: measure, recommend, look at Ollama. Never downloads or writes."""
        if self.running:
            return self.status()

        async def never(_question: Question) -> bool:
            return False

        self.state = SetupState()
        await run_setup(self._deps(), confirm=never, dry_run=True, state=self.state)
        return self.status()

    def start(self, *, install_ollama: bool) -> dict[str, Any]:
        if self.running:
            return {"ok": False, "reason": "running", **self.status()}

        async def confirm(question: Question) -> bool:
            if question is Question.INSTALL_OLLAMA:
                return install_ollama
            return True  # the click on "Einrichten" was the consent to the download

        self.state = SetupState()
        self.state.running = True
        self._task = asyncio.get_running_loop().create_task(
            run_setup(self._deps(), confirm=confirm, state=self.state), name="nox-localai-setup"
        )
        log.info("localai.setup_started", install_ollama=install_ollama)
        return {"ok": True, **self.status()}

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def register_localai_ipc(registry: RequestRegistry, service: LocalAiService) -> None:
    async def status(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        return service.status()

    async def check(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        return await service.check()

    async def start(ctx: RequestContext, payload: SetupStart) -> dict[str, Any]:
        log.info("localai.setup_requested", by=ctx.role)
        return service.start(install_ollama=payload.install_ollama)

    registry.register("ai.setup.status", EmptyPayload, status, roles=LOCAL_UI_ROLES)
    registry.register("ai.setup.check", EmptyPayload, check, roles=LOCAL_UI_ROLES)
    registry.register("ai.setup.start", SetupStart, start, roles=LOCAL_UI_ROLES)
