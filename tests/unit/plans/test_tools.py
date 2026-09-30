"""The four `plans.*` tools, and the two properties that make them safe to offer a model.

First: proposing writes nothing anywhere. Second: the identifier is the core's, never the model's -
an id from outside could name a plan the user is in the middle of approving.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from nox.plans.book import PlanBook
from nox.plans.tools import register_plan_tools
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec
from tests.unit.plans.doubles import FakeQueue, FakeTasks


class _Input(BaseModel):
    pass


async def _noop(_payload: dict[str, Any]) -> dict[str, Any]:
    return {}


@pytest.fixture
def registry() -> ToolRegistry:
    tools = ToolRegistry()
    for name in ("time.now", "vault.read"):
        tools.register(
            ToolSpec(
                name=name,
                description=f"does {name}",
                input_model=_Input,
                risk=Risk.READ,
                handler=_noop,
                side_effects=False,
            )
        )
    return tools


@pytest.fixture
def book() -> PlanBook:
    return PlanBook(queue=FakeQueue(), tasks=FakeTasks())


@pytest.fixture
def handlers(registry: ToolRegistry, book: PlanBook) -> dict[str, Any]:
    register_plan_tools(registry, book)
    return {name: registry.get(name).handler for name in registry.names()}


def steps(*tools: str) -> list[dict[str, Any]]:
    return [{"tool": tool, "why": f"weil {tool}"} for tool in tools]


async def test_proposing_returns_a_numbered_plan_and_runs_nothing(
    handlers: dict[str, Any], book: PlanBook
) -> None:
    answer = await handlers["plans.propose"](
        {"title": "Aufraeumen", "steps": steps("time.now", "vault.read")}
    )

    assert answer["ok"]
    assert [step["step"] for step in answer["plan"]["steps"]] == [0, 1]
    assert [step["why"] for step in answer["plan"]["steps"]] == ["weil time.now", "weil vault.read"]
    assert book.queue.submitted == [], "proposing must not reach the queue"


async def test_a_tool_the_model_invented_is_refused_with_the_real_list(
    handlers: dict[str, Any], book: PlanBook
) -> None:
    answer = await handlers["plans.propose"](
        {"title": "Loeschen", "steps": steps("file.delete", "time.now")}
    )

    assert answer["ok"] is False
    assert "file.delete" in answer["error"]
    assert "time.now" in answer["available"]
    assert book.waiting() == [], "a plan with an unknown step must not be stored at all"


async def test_the_identifier_belongs_to_the_core(handlers: dict[str, Any]) -> None:
    first = await handlers["plans.propose"]({"title": "Gleich", "steps": steps("time.now")})
    second = await handlers["plans.propose"]({"title": "Gleich", "steps": steps("time.now")})

    assert first["plan"]["id"] != second["plan"]["id"]


async def test_starting_asks_the_user_and_says_so(registry: ToolRegistry, book: PlanBook) -> None:
    """High risk means the permission engine's default is confirm in every profile."""
    register_plan_tools(registry, book)
    spec = registry.get("plans.start")

    assert spec is not None
    assert spec.risk is Risk.HIGH
    assert spec.side_effects


async def test_the_confirmation_dialog_shows_the_title_not_the_id(
    handlers: dict[str, Any], registry: ToolRegistry
) -> None:
    """A person can answer "Start: Downloads sortieren". They cannot answer "start: 8f2c1a"."""
    proposed = await handlers["plans.propose"](
        {"title": "Downloads sortieren", "steps": steps("time.now")}
    )
    targets = registry.get("plans.start").targets

    assert targets is not None
    assert targets({"plan": proposed["plan"]["id"]}) == "Downloads sortieren"


async def test_starting_a_plan_that_is_not_waiting_says_so(handlers: dict[str, Any]) -> None:
    answer = await handlers["plans.start"]({"plan": "ghost"})

    assert answer["ok"] is False and "ghost" in answer["error"]


async def test_starting_hands_it_over(handlers: dict[str, Any], book: PlanBook) -> None:
    proposed = await handlers["plans.propose"]({"title": "Jetzt", "steps": steps("time.now")})

    answer = await handlers["plans.start"]({"plan": proposed["plan"]["id"]})

    assert answer["ok"] and answer["task"] == "t1"
    assert len(book.queue.submitted) == 1


async def test_waiting_lists_what_needs_a_decision(handlers: dict[str, Any]) -> None:
    await handlers["plans.propose"]({"title": "Eins", "steps": steps("time.now")})

    answer = await handlers["plans.waiting"]({})

    assert [plan["title"] for plan in answer["waiting"]] == ["Eins"]


async def test_the_status_of_something_never_started_is_an_answer_not_a_crash(
    handlers: dict[str, Any],
) -> None:
    answer = await handlers["plans.status"]({"plan": "ghost"})

    assert answer["ok"] is False


async def test_the_risk_levels_match_what_the_tools_actually_do(
    registry: ToolRegistry, book: PlanBook
) -> None:
    """Risk is what the permission engine decides on, so a wrong label hands out a permission."""
    register_plan_tools(registry, book)

    for name in ("plans.waiting", "plans.status"):
        spec = registry.get(name)
        assert spec.risk is Risk.READ and not spec.side_effects, name
    assert registry.get("plans.propose").risk is Risk.LOW
    assert not registry.get("plans.propose").side_effects
