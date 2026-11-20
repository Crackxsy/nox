"""`Orchestrator.add_fact_source`: extensions add lines after the core's own facts, in order."""

from __future__ import annotations

import pytest

from nox.core.hooks import DuplicateHookError
from tests.unit.fakes import make_orchestrator


def test_sources_follow_the_cores_facts_in_the_order_they_were_added() -> None:
    orch, *_ = make_orchestrator()
    orch.facts = lambda: "It is noon."
    orch.add_fact_source("devices", lambda: "Devices: a laptop.")
    orch.add_fact_source("weather", lambda: "Rain later.")
    assert orch._facts() == "It is noon.\nDevices: a laptop.\nRain later."


def test_a_source_with_nothing_to_say_adds_no_empty_line() -> None:
    orch, *_ = make_orchestrator()
    orch.facts = lambda: "It is noon."
    orch.add_fact_source("quiet", lambda: "")
    orch.add_fact_source("loud", lambda: "Rain later.")
    assert orch._facts() == "It is noon.\nRain later."


def test_sources_are_the_facts_when_the_core_has_none() -> None:
    orch, *_ = make_orchestrator()
    orch.facts = None
    assert orch._facts() == ""
    orch.add_fact_source("devices", lambda: "Devices: a laptop.")
    assert orch._facts() == "Devices: a laptop."


def test_a_second_source_under_the_same_name_is_refused_not_shown_twice() -> None:
    orch, *_ = make_orchestrator()
    orch.facts = lambda: "It is noon."
    orch.add_fact_source("devices", lambda: "Devices: a laptop.")
    with pytest.raises(DuplicateHookError):
        orch.add_fact_source("devices", lambda: "Devices: a laptop.")
    assert orch._facts() == "It is noon.\nDevices: a laptop."


def test_a_failing_source_costs_the_facts_and_never_the_turn() -> None:
    orch, *_ = make_orchestrator()
    orch.facts = lambda: "It is noon."

    def broken() -> str:
        raise RuntimeError("no line today")

    orch.add_fact_source("devices", broken)
    assert orch._facts() == ""


def test_a_source_is_asked_again_for_every_turn() -> None:
    orch, *_ = make_orchestrator()
    lines = iter(["first", "second"])
    orch.add_fact_source("counter", lambda: next(lines))
    assert [orch._facts(), orch._facts()] == ["first", "second"]
