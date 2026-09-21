"""`NoxCore._providers_json` (the `ai.providers` response) must answer inside the UI's request
timeout.

The dashboard's "KI-Anbieter" card sat on "Keine Anbieterliste empfangen" while the Status page
listed `ai.rules`/`ai.ollama`/`ai.claude_code` from the very same probes:
`DefaultRouter.providers()` probes every provider live, `claude_code` is allowed 15 s, and the UI
gives a request 10 s (`ui/shared/ipc.ts`). The payload shape was never the problem, the latency was.
"""

from __future__ import annotations

from types import SimpleNamespace

import nox.app as app
from nox.ai.base import ProviderInfo
from nox.core.boot.ai import ProviderCard
from nox.core.config import NoxConfig
from nox.core.events import HealthChanged, HealthStatus


def make_info(pid: str, status: HealthStatus = HealthStatus.UNAVAILABLE) -> ProviderInfo:
    return ProviderInfo(
        id=pid, display_name=pid.title(), local=True, roles=["chat"], status=status, reason=""
    )


class _Provider:
    def __init__(self, info: ProviderInfo) -> None:
        self.info = info


class _Router:
    """Stands in for `DefaultRouter`: `providers()` probes live and may take a long time."""

    def __init__(self, infos: list[ProviderInfo], *, delay_s: float = 0.0) -> None:
        self._infos = infos
        self._delay = delay_s

    async def providers(self) -> list[ProviderInfo]:
        if self._delay:
            import asyncio  # noqa: PLC0415 - local to the fake

            await asyncio.sleep(self._delay)
        return self._infos


#: Short enough that the slow-probe test does not wait, long enough that the fast one wins.
PROBE_BUDGET_S = 0.05


def make_core(router: _Router, health: dict[str, HealthChanged]) -> app.NoxCore:
    """A core with only the three pieces the provider card reads, wired as `_build_models` does."""
    core = app.NoxCore(NoxConfig(), voice=False, extensions=False)
    core.router = router  # type: ignore[assignment]
    core.ai_providers = [_Provider(make_info("rules")), _Provider(make_info("claude_code"))]  # type: ignore[list-item]
    core.health = SimpleNamespace(current=lambda: health)  # type: ignore[assignment]
    core.provider_card = ProviderCard(
        core.ai_providers,
        probe=core._probe_providers,
        health_entry=core._health_entry,
        budget_s=PROBE_BUDGET_S,
    )
    return core


async def test_live_probe_result_is_used_when_it_answers_in_time() -> None:
    live = [make_info("rules", HealthStatus.AVAILABLE)]
    core = make_core(_Router(live), {})
    payload = await core.providers_json()
    assert [p["id"] for p in payload] == ["rules"]
    assert payload[0]["status"] == "available"


async def test_slow_probe_falls_back_to_the_health_report_not_to_nothing() -> None:
    health = {
        "ai.rules": HealthChanged(
            component="ai.rules", status=HealthStatus.AVAILABLE, reason="rule set loaded"
        ),
        "ai.claude_code": HealthChanged(
            component="ai.claude_code", status=HealthStatus.UNAVAILABLE, reason="timeout after 15s"
        ),
    }
    core = make_core(_Router([], delay_s=5.0), health)
    payload = await core.providers_json()
    assert [p["id"] for p in payload] == ["rules", "claude_code"]  # never an empty list
    assert payload[0]["status"] == "available" and payload[0]["reason"] == "rule set loaded"
    assert payload[1]["status"] == "unavailable"
    # The dashboard parses exactly these keys (`ui/dashboard/src/model.ts::parseProviders`).
    assert {"id", "display_name", "local", "roles", "status", "reason"} <= set(payload[0])
