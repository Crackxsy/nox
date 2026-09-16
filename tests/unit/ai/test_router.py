"""DefaultRouter: chain, fallback, privacy filter, health cache, events, explain, budget, stream."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from nox.ai.base import AiRole
from nox.ai.config import RouterConfig
from nox.ai.errors import BudgetExceededError, NoProviderAvailableError
from nox.ai.router import DefaultRouter
from nox.core.events import E, HealthStatus

from .conftest import FakeBus, FakeProvider, make_request


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build(
    bus: FakeBus,
    *providers: FakeProvider,
    config: RouterConfig | None = None,
    cloud_allowed: bool = True,
    clock: Clock | None = None,
    today: date | None = None,
) -> DefaultRouter:
    cfg = config or RouterConfig(fallback_chain=[p.pid for p in providers])
    return DefaultRouter(
        list(providers),
        bus,
        cfg,
        cloud_allowed=lambda: cloud_allowed,
        clock=clock or Clock(),
        today=lambda: today or date(2026, 9, 9),
    )


async def test_first_choice_is_not_degraded_and_emits_events(bus: FakeBus) -> None:
    cloud = FakeProvider("claude_code", local=False, text="hi")
    local = FakeProvider("ollama")
    router = build(bus, cloud, local)
    response = await router.complete(make_request())
    assert response.provider == "claude_code"
    assert response.degraded is False
    assert bus.names() == [E.AI_REQUEST_STARTED, E.AI_RESPONSE_READY]
    assert bus.of(E.AI_REQUEST_STARTED)[0].payload["provider"] == "claude_code"
    assert bus.of(E.AI_RESPONSE_READY)[0].payload["text"] == "hi"
    assert "=> claude_code" in router.explain("req-1")


async def test_fallback_on_failure_marks_degraded_and_emits_change(bus: FakeBus) -> None:
    cloud = FakeProvider("claude_code", local=False, error=RuntimeError("boom"))
    local = FakeProvider("ollama", text="local answer")
    router = build(bus, cloud, local)
    response = await router.complete(make_request())
    assert response.provider == "ollama"
    assert response.degraded is True
    assert response.degraded_reason == "fallback:claude_code->ollama"
    failed = bus.of(E.AI_REQUEST_FAILED)[0].payload
    assert failed["provider"] == "claude_code"
    assert failed["fallback_to"] == "ollama"
    changed = bus.of(E.AI_PROVIDER_CHANGED)[0].payload
    assert (changed["previous"], changed["current"]) == ("claude_code", "ollama")
    explanation = router.explain("req-1")
    assert "claude_code: failed" in explanation and "=> ollama (degraded" in explanation


async def test_provider_degraded_reason_is_kept_when_falling_back(bus: FakeBus) -> None:
    cloud = FakeProvider("claude_code", local=False, error=RuntimeError("x"))
    rules = FakeProvider("rules", degraded=True, degraded_reason="rules")
    router = build(bus, cloud, rules)
    response = await router.complete(make_request())
    assert response.degraded_reason == "fallback:claude_code->rules;rules"


@pytest.mark.parametrize("privacy_mode", ["private", "offline"])
async def test_privacy_mode_filters_cloud_providers(bus: FakeBus, privacy_mode: str) -> None:
    cloud = FakeProvider("claude_code", local=False)
    local = FakeProvider("ollama", text="local")
    router = build(bus, cloud, local)
    response = await router.complete(make_request(privacy_mode=privacy_mode))
    assert response.provider == "ollama"
    assert cloud.complete_calls == 0
    assert f"cloud blocked by privacy mode {privacy_mode}" in router.explain("req-1")


async def test_cloud_allowed_callable_filters_cloud(bus: FakeBus) -> None:
    cloud = FakeProvider("claude_code", local=False)
    local = FakeProvider("ollama")
    router = build(bus, cloud, local, cloud_allowed=False)
    response = await router.complete(make_request())
    assert response.provider == "ollama"
    assert "cloud blocked by profile" in router.explain("req-1")


async def test_no_provider_left_raises_and_emits_failed(bus: FakeBus) -> None:
    cloud = FakeProvider("claude_code", local=False)
    router = build(bus, cloud)
    with pytest.raises(NoProviderAvailableError):
        await router.complete(make_request(privacy_mode="private"))
    failed = bus.of(E.AI_REQUEST_FAILED)[0].payload
    assert failed["provider"] == "router" and failed["fallback_to"] is None


async def test_unavailable_health_is_skipped_and_reprobed_after_ttl(bus: FakeBus) -> None:
    clock = Clock()
    flaky = FakeProvider("ollama", status=HealthStatus.UNAVAILABLE)
    rules = FakeProvider("rules")
    router = build(bus, flaky, rules, clock=clock)
    first = await router.complete(make_request(request_id="a"))
    assert first.provider == "rules" and flaky.health_calls == 1
    second = await router.complete(make_request(request_id="b"))
    assert second.provider == "rules" and flaky.health_calls == 1  # cached, not re-probed
    flaky.status = HealthStatus.AVAILABLE
    clock.now += 61
    third = await router.complete(make_request(request_id="c"))
    assert third.provider == "ollama" and flaky.health_calls == 2


async def test_failed_provider_is_marked_unavailable_until_reprobe(bus: FakeBus) -> None:
    clock = Clock()
    cloud = FakeProvider("claude_code", local=False, error=RuntimeError("down"))
    local = FakeProvider("ollama")
    router = build(bus, cloud, local, clock=clock)
    await router.complete(make_request(request_id="a"))
    assert cloud.complete_calls == 1
    await router.complete(make_request(request_id="b"))
    assert cloud.complete_calls == 1  # skipped without trying
    assert "unavailable" in router.explain("b")
    cloud.error = None
    clock.now += 61
    response = await router.complete(make_request(request_id="c"))
    assert response.provider == "claude_code"


async def test_health_exception_counts_as_unavailable(bus: FakeBus) -> None:
    broken = FakeProvider("ollama", health_raises=True)
    rules = FakeProvider("rules")
    router = build(bus, broken, rules)
    response = await router.complete(make_request())
    assert response.provider == "rules"
    infos = await router.providers()
    assert {i.id: i.status for i in infos}["ollama"] is HealthStatus.UNAVAILABLE


async def test_role_not_supported_is_skipped(bus: FakeBus) -> None:
    chatty = FakeProvider("claude_code", local=False, roles=[AiRole.CODE])
    rules = FakeProvider("rules")
    router = build(bus, chatty, rules)
    response = await router.complete(make_request())
    assert response.provider == "rules"
    assert "role chat not supported" in router.explain("req-1")


async def test_timeout_falls_through_to_next_provider(bus: FakeBus) -> None:
    slow = FakeProvider("claude_code", local=False, delay_s=0.3)
    fast = FakeProvider("ollama", text="fast")
    router = build(bus, slow, fast)
    response = await router.complete(make_request(timeout_s=0.05))
    assert response.provider == "ollama"
    assert "timeout" in bus.of(E.AI_REQUEST_FAILED)[0].payload["error"]


async def test_all_fail_raises(bus: FakeBus) -> None:
    a = FakeProvider("claude_code", local=False, error=RuntimeError("a"))
    b = FakeProvider("ollama", error=RuntimeError("b"))
    router = build(bus, a, b)
    with pytest.raises(NoProviderAvailableError):
        await router.complete(make_request())
    assert len(bus.of(E.AI_REQUEST_FAILED)) == 2
    assert "all candidates failed" in router.explain("req-1")


async def test_stream_yields_chunks_and_emits_ready(bus: FakeBus) -> None:
    provider = FakeProvider("ollama", chunks=["Hal", "lo"])
    router = build(bus, provider)
    chunks = [c async for c in router.stream(make_request())]
    assert "".join(c.delta for c in chunks) == "Hallo"
    assert chunks[-1].done is True
    assert len(bus.of(E.AI_RESPONSE_CHUNK)) == 3
    ready = bus.of(E.AI_RESPONSE_READY)[0].payload
    assert ready["text"] == "Hallo" and ready["provider"] == "ollama"
    assert ready["tokens_in"] == 10


async def test_stream_falls_back_before_first_chunk(bus: FakeBus) -> None:
    broken = FakeProvider("claude_code", local=False, error=RuntimeError("no"))
    good = FakeProvider("ollama", chunks=["ok"])
    router = build(bus, broken, good)
    chunks = [c async for c in router.stream(make_request())]
    assert "".join(c.delta for c in chunks) == "ok"
    assert bus.of(E.AI_PROVIDER_CHANGED)[0].payload["current"] == "ollama"
    assert bus.of(E.AI_RESPONSE_READY)[0].payload["degraded"] is True


async def test_stream_does_not_fall_back_after_first_chunk(bus: FakeBus) -> None:
    broken = FakeProvider(
        "claude_code",
        local=False,
        chunks=["a", "b"],
        error=RuntimeError("mid"),
        fail_after_chunks=1,
    )
    good = FakeProvider("ollama", chunks=["ok"])
    router = build(bus, broken, good)
    received: list[str] = []
    with pytest.raises(NoProviderAvailableError):
        async for chunk in router.stream(make_request()):
            received.append(chunk.delta)
    assert received == ["a"]
    assert good.stream_calls == 0
    assert bus.of(E.AI_REQUEST_FAILED)[0].payload["fallback_to"] is None


async def test_stream_timeout_falls_back(bus: FakeBus) -> None:
    slow = FakeProvider("claude_code", local=False, delay_s=0.3)
    fast = FakeProvider("ollama", chunks=["fast"])
    router = build(bus, slow, fast)
    chunks = [c async for c in router.stream(make_request(timeout_s=0.05))]
    assert "".join(c.delta for c in chunks) == "fast"


async def test_stream_cancellation_propagates(bus: FakeBus) -> None:
    slow = FakeProvider("ollama", delay_s=5)
    router = build(bus, slow)

    async def consume() -> None:
        async for _ in router.stream(make_request(timeout_s=30)):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_explain_unknown_and_history_limit(bus: FakeBus) -> None:
    provider = FakeProvider("rules")
    router = DefaultRouter([provider], bus, RouterConfig(fallback_chain=["rules"]), max_decisions=3)
    assert "no routing decision" in router.explain("nope")
    for i in range(5):
        await router.complete(make_request(request_id=f"r{i}"))
    assert "no routing decision" in router.explain("r0")
    assert "=> rules" in router.explain("r4")
    assert len(router.decisions()) == 3


async def test_chain_per_role_from_config(bus: FakeBus) -> None:
    cloud = FakeProvider("claude_code", local=False)
    local = FakeProvider("ollama")
    rules = FakeProvider("rules")
    cfg = RouterConfig(fallback_chain=["claude_code", "ollama", "rules"])
    router = build(bus, cloud, local, rules, config=cfg)
    assert (await router.complete(make_request(role=AiRole.CLASSIFY))).provider == "rules"
    assert (await router.complete(make_request(role=AiRole.CODE))).provider == "claude_code"
    cfg2 = RouterConfig(fallback_chain=["claude_code", "ollama"], roles={"chat": ["ollama"]})
    router2 = build(bus, cloud, local, config=cfg2)
    assert (await router2.complete(make_request())).provider == "ollama"


async def test_background_budget_share_skips_cloud_then_refuses(bus: FakeBus) -> None:
    cloud = FakeProvider("claude_code", local=False, tokens=(100, 100))
    local = FakeProvider("ollama", tokens=(100, 100))
    cfg = RouterConfig(
        fallback_chain=["claude_code", "ollama"],
        background_budget_share=0.30,
        budget_warmup_tokens=0,
    )
    router = build(bus, cloud, local, config=cfg)
    # 200 cloud tokens for chat, then 200 cloud tokens for background => 50 % share.
    await router.complete(make_request(request_id="c1"))
    bg1 = await router.complete(make_request(request_id="b1", role=AiRole.BACKGROUND))
    assert bg1.provider == "claude_code"
    assert router.usage_today()["cloud"] == {"chat": 200, "background": 200}
    bg2 = await router.complete(make_request(request_id="b2", role=AiRole.BACKGROUND))
    assert bg2.provider == "ollama" and bg2.degraded is True
    assert "background cloud budget exhausted" in router.explain("b2")
    # Chat is never budget-limited.
    assert (await router.complete(make_request(request_id="c2"))).provider == "claude_code"
    # Without a local provider the background request is refused.
    only_cloud = DefaultRouter(
        [cloud], bus, cfg.model_copy(update={"fallback_chain": ["claude_code"]})
    )
    await only_cloud.complete(make_request(request_id="x1"))
    only_cloud._usage.cloud_by_role["background"] = 1000
    with pytest.raises(BudgetExceededError):
        await only_cloud.complete(make_request(request_id="x2", role=AiRole.BACKGROUND))


async def test_budget_warmup_allows_first_requests(bus: FakeBus) -> None:
    cloud = FakeProvider("claude_code", local=False, tokens=(10, 10))
    cfg = RouterConfig(fallback_chain=["claude_code"], budget_warmup_tokens=2000)
    router = build(bus, cloud, config=cfg)
    for i in range(5):
        response = await router.complete(make_request(request_id=f"b{i}", role=AiRole.BACKGROUND))
        assert response.provider == "claude_code"


async def test_usage_resets_on_new_day(bus: FakeBus) -> None:
    day = {"value": date(2026, 9, 9)}
    cloud = FakeProvider("claude_code", local=False, tokens=(50, 50))
    router = DefaultRouter(
        [cloud], bus, RouterConfig(fallback_chain=["claude_code"]), today=lambda: day["value"]
    )
    await router.complete(make_request())
    assert router.usage_today()["all"] == {"chat": 100}
    day["value"] = date(2026, 9, 10)
    assert router.usage_today()["all"] == {}
