"""What the catalogue says, and - the part that matters - what it refuses to say.

The interesting assertions here are the ones about not knowing. An assistant that turns "I have no
entry for that" into "no, I cannot" is worse than one that says nothing, because the user believes
it. So `capabilities.check` is tested three ways: a tool it has, a gap it knows about, and a name
it has never heard of.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from nox.capabilities.catalog import build_report, stale_gaps
from nox.capabilities.gaps import CURATED_GAPS, derived_gaps
from nox.capabilities.model import CapabilityState, GapReason
from nox.capabilities.tools import register_capability_tools
from nox.security.model import (
    Decision,
    PermissionRequest,
    PermissionResult,
    Profile,
    ProfileRule,
    Risk,
)
from nox.tools.registry import ToolRegistry, ToolSpec


class _Input(BaseModel):
    pass


async def _handler(_payload: dict[str, Any]) -> dict[str, Any]:
    return {}


def spec(name: str, risk: Risk = Risk.READ) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"does {name}",
        input_model=_Input,
        risk=risk,
        handler=_handler,
        side_effects=risk is not Risk.READ,
    )


class FakeEngine:
    """Answers from a table and records what it was asked - the asking is half the contract."""

    def __init__(self, profile: Profile, decisions: dict[str, Decision] | None = None) -> None:
        self._profile = profile
        self._decisions = decisions or {}
        self.asked: list[PermissionRequest] = []

    def preview(self, request: PermissionRequest) -> PermissionResult:
        self.asked.append(request)
        name = f"{request.tool}.{request.action}" if request.action else request.tool
        decision = self._decisions.get(name, Decision.ALLOW)
        return PermissionResult(decision=decision, rule_id=f"fake.{decision.value}")

    def active_profile(self) -> Profile:
        return self._profile


def profile(*rules: ProfileRule) -> Profile:
    return Profile(id="test", rules=list(rules))


@pytest.fixture
def registry() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(spec("time.now"))
    tools.register(spec("home.light", Risk.MEDIUM))
    tools.register(spec("vault.read"))
    return tools


def report_for(tools: ToolRegistry, engine: FakeEngine, **kwargs: Any) -> Any:
    return build_report(
        tools=tools, engine=engine, mode="companion", privacy_mode="balanced", **kwargs
    )


def handlers(tools: ToolRegistry) -> dict[str, Any]:
    return {name: tools.get(name).handler for name in tools.names()}


def test_every_registered_tool_is_in_the_report(registry: ToolRegistry) -> None:
    report = report_for(registry, FakeEngine(profile()))

    assert {c.name for c in report.capabilities} == {"time.now", "home.light", "vault.read"}


def test_the_tool_name_is_split_the_way_the_executor_splits_it(registry: ToolRegistry) -> None:
    """A rule written for tool `home` must match `home.light`, so the request has to be split.

    This is the mistake that made every preset step come back denied: the unsplit name was passed
    as the tool, no rule matched, and the probe that was supposed to prove it reported `allow`.
    """
    engine = FakeEngine(profile())

    report_for(registry, engine)

    assert ("home", "light") in [(r.tool, r.action) for r in engine.asked]


def test_a_tool_that_would_ask_first_is_not_usable(registry: ToolRegistry) -> None:
    engine = FakeEngine(profile(), {"home.light": Decision.CONFIRM})

    report = report_for(registry, engine)
    home = report.find("home.light")

    assert home is not None and not home.usable
    assert [c.name for c in report.usable] == ["time.now", "vault.read"]


def test_a_tool_without_its_credential_is_not_usable(registry: ToolRegistry) -> None:
    """Allowed and unreachable is the state that makes an assistant promise and then fail."""

    def probe(name: str) -> tuple[CapabilityState, str] | None:
        if name.startswith("home."):
            return CapabilityState.UNAVAILABLE, "no token stored"
        return None

    report = report_for(registry, FakeEngine(profile()), probe=probe)
    home = report.find("home.light")

    assert home is not None
    assert home.decision is Decision.ALLOW and not home.usable
    assert home.reason == "no token stored"


def test_a_target_dependent_rule_is_marked(registry: ToolRegistry) -> None:
    """A yes that only holds inside the vault must not be reported as a plain yes."""
    narrowed = profile(
        ProfileRule(
            id="vault.research", tool="vault*", target="*Research*", decision=Decision.ALLOW
        )
    )

    report = report_for(registry, FakeEngine(narrowed))

    assert report.find("vault.read").target_dependent
    assert not report.find("time.now").target_dependent


def test_a_gap_whose_tool_now_exists_is_dropped(registry: ToolRegistry) -> None:
    """The maintenance failure this catches: a capability ships and the "I cannot" stays behind.

    It has already caught one. `file.delete` was the example here until the file tools shipped, and
    this test failed until the row was taken out of the table - which is the whole point of it.
    """
    registry.register(spec("window.manage", Risk.MEDIUM))

    report = report_for(registry, FakeEngine(profile()))

    assert stale_gaps(registry) == ["window.manage"]
    assert "window.manage" not in {gap.name for gap in report.gaps}


def test_the_curated_gaps_do_not_name_registered_tools(registry: ToolRegistry) -> None:
    assert stale_gaps(registry) == []


def test_a_plugin_that_is_installed_but_off_becomes_a_gap() -> None:
    gaps = derived_gaps(installed_plugins=["home", "obs"], enabled_plugins=["obs"])

    assert [(gap.name, gap.reason) for gap in gaps] == [("home.*", GapReason.DISABLED)]


def test_a_boundary_is_not_filed_as_a_backlog_item() -> None:
    """A thing Nox will never do and a thing it does not do yet are different answers."""
    forbidden = {gap.name for gap in CURATED_GAPS if gap.reason is GapReason.FORBIDDEN}

    assert forbidden == {"home.lock", "game.input"}


async def test_check_answers_for_a_tool_it_has(registry: ToolRegistry) -> None:
    report = report_for(registry, FakeEngine(profile()))
    register_capability_tools(registry, lambda: report)

    answer = await handlers(registry)["capabilities.check"]({"name": "vault.read"})

    assert answer["known"] and answer["possible"]


async def test_check_answers_for_something_it_cannot_do(registry: ToolRegistry) -> None:
    report = report_for(registry, FakeEngine(profile()))
    register_capability_tools(registry, lambda: report)

    answer = await handlers(registry)["capabilities.check"]({"name": "window.manage"})

    assert answer["known"] and answer["possible"] is False
    assert answer["why"] == GapReason.MISSING.value


async def test_check_says_it_does_not_know_instead_of_saying_no(registry: ToolRegistry) -> None:
    """The whole point of the tool. An unknown name is not a denial."""
    report = report_for(registry, FakeEngine(profile()))
    register_capability_tools(registry, lambda: report)

    answer = await handlers(registry)["capabilities.check"]({"name": "order.pizza"})

    assert answer["known"] is False
    assert "possible" not in answer
    assert "cannot say" in answer["note"]


async def test_the_list_separates_usable_from_asks_first(registry: ToolRegistry) -> None:
    engine = FakeEngine(profile(), {"home.light": Decision.CONFIRM, "vault.read": Decision.DENY})
    report = report_for(registry, engine)
    register_capability_tools(registry, lambda: report)

    summary = await handlers(registry)["capabilities.list"]({})

    assert summary["usable"] == ["time.now"]
    assert [entry["name"] for entry in summary["asks_first"]] == ["home.light"]
    assert [entry["name"] for entry in summary["refused_here"]] == ["vault.read"]
    assert summary["missing"], "the gaps have to reach the model, not only the dashboard"
