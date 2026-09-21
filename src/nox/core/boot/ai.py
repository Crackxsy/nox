"""Building the language-model providers and the router in front of them.

Every provider that speaks HTTP is constructed with a client factory from the egress guard, so
there is exactly one way out of this process and the privacy mode decides whether even the local
model server is reachable.

`ProviderCard` answers the dashboard's provider card. The live probe gets a short budget, and
whatever it does not deliver in time is answered from the health service's last observation of the
very same providers. A card that says "checked a moment ago" is honest; a card that says nothing
because a slow probe outran the UI's request timeout is not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from nox.ai.base import AiProvider, ProviderInfo
from nox.ai.config import AiConfig
from nox.ai.providers.claude_code import ClaudeCodeProvider
from nox.ai.providers.ollama import OllamaProvider
from nox.ai.providers.rules import RulesProvider
from nox.ai.router import DefaultRouter
from nox.core.events import EventBus
from nox.core.logging import get_logger
from nox.security.egress import EgressGuard

log = get_logger(__name__)

__all__ = ["PROVIDERS_PROBE_BUDGET_S", "ProviderCard", "build_providers", "build_router"]

#: Budget for the live provider probe behind `ai.providers`, chosen well below the UI clients'
#: 10 s request timeout. A slower probe is answered from health instead.
PROVIDERS_PROBE_BUDGET_S = 3.0

#: The provider clients' own timeouts: a connection that has not succeeded in 5 s will not, and a
#: request that has not answered in 10 s is past what a chat turn can wait for.
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def build_providers(
    ai_config: AiConfig,
    *,
    egress: EgressGuard,
    status_source: Callable[[], Mapping[str, Any]],
) -> list[AiProvider]:
    """The provider chain, in fallback order, each with its network access already guarded."""
    return [
        RulesProvider(status_source=lambda: dict(status_source())),
        OllamaProvider(
            ai_config.providers.ollama,
            client_factory=lambda: egress.client(timeout=_HTTP_TIMEOUT),
        ),
        ClaudeCodeProvider(ai_config.providers.claude_code),
    ]


def build_router(
    providers: list[AiProvider],
    ai_config: AiConfig,
    *,
    bus: EventBus,
    cloud_allowed: Callable[[], bool],
) -> DefaultRouter:
    return DefaultRouter(providers, bus, ai_config.router, cloud_allowed=cloud_allowed)


class ProviderCard:
    """Answers `ai.providers` from a live probe, falling back to the last health observation."""

    def __init__(
        self,
        providers: list[AiProvider],
        *,
        probe: Callable[[], Any],
        health_entry: Callable[[str], Any],
        budget_s: float = PROVIDERS_PROBE_BUDGET_S,
    ) -> None:
        self._providers = providers
        self._probe = probe
        self._health_entry = health_entry
        self._budget_s = budget_s
        self._running: asyncio.Task[list[ProviderInfo]] | None = None

    async def read(self) -> list[dict[str, Any]]:
        infos = await self._infos()
        return [info.model_dump(mode="json") for info in infos]

    async def cancel(self) -> None:
        """Stop a probe that is still running, during shutdown."""
        task, self._running = self._running, None
        if task is not None and not task.done():
            task.cancel()

    async def _infos(self) -> list[ProviderInfo]:
        task = self._running
        if task is None or task.done():
            # Shielded and left running: a probe that misses the budget still finishes and fills
            # the router's health cache, so the next request is answered from live data.
            task = asyncio.ensure_future(self._probe())
            task.add_done_callback(_retrieve_exception)
            self._running = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), self._budget_s)
        except TimeoutError:
            log.warning("ai.providers_probe_slow", budget_s=self._budget_s)
        except Exception as exc:  # noqa: BLE001 - a broken probe must still answer the UI
            log.warning("ai.providers_probe_failed", error=f"{type(exc).__name__}: {exc}")
        return [self._from_health(provider) for provider in self._providers]

    def _from_health(self, provider: AiProvider) -> ProviderInfo:
        """The provider's static info plus the last `ai.<id>` health result, or its own status."""
        entry = self._health_entry(f"ai.{provider.info.id}")
        if entry is None:
            return provider.info
        return provider.info.model_copy(update={"status": entry.status, "reason": entry.reason})


def _retrieve_exception(task: asyncio.Task[Any]) -> None:
    """Consume a background task's exception so asyncio does not report it as never retrieved."""
    if not task.cancelled():
        task.exception()
