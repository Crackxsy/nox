"""What the core remembers for a node: a write is never remembered, and a profile rule
allows a call on a node only when it names exactly that device."""

from __future__ import annotations

from typing import Any

import pytest

from nox.node.hands import WriteArgs
from nox.node.router import routed_spec
from nox.security.model import Risk
from tests.unit.node.support import FakeRegistry, registry_entry


async def test_a_node_write_is_high_when_it_replaces_and_never_remembered() -> None:
    from nox.node.install import _write_risk, _write_target
    from nox.security.model import Decision, PermissionResult
    from nox.tools.executor import ToolExecutor
    from nox.tools.registry import ToolRegistry

    laptop = registry_entry("a" * 32, "Laptop", tools={"file.write": "medium"})
    laptop.confirmer = True
    registry = FakeRegistry([laptop])
    spec = routed_spec(
        name="file.write",
        description="t",
        risk=Risk.MEDIUM,
        side_effects=True,
        registry=registry,  # type: ignore[arg-type]
        input_model=WriteArgs,
        node_target=_write_target,
        node_risk=_write_risk,
        remember_on_node=False,
    )
    payload = {"path": "a.txt", "text": "neu", "device": "Laptop"}
    new = spec.scope(spec.input_model.model_validate(payload).model_dump())  # type: ignore[misc]
    assert new.risk is Risk.MEDIUM and not new.rememberable
    assert '"new file"' in new.target and "bytes 3" in new.target
    over = {**payload, "overwrite": True}
    replaced = spec.scope(spec.input_model.model_validate(over).model_dump())  # type: ignore[misc]
    assert replaced.risk is Risk.HIGH and replaced.confirm_on_device and not replaced.rememberable

    seen: list[Any] = []

    class Engine:
        def check(self, request: Any) -> PermissionResult:
            seen.append(request)
            return PermissionResult(decision=Decision.DENY, rule_id="test")

        def is_tainted(self, conversation: str) -> bool:
            return False

    class Audit:
        def append(self, **_k: Any) -> int:
            return 0

    class Kill:
        def is_engaged(self) -> bool:
            return False

    tools = ToolRegistry()
    tools.register(spec)
    executor = ToolExecutor(tools, Engine(), Audit(), None, Kill())  # type: ignore[arg-type]
    await executor.call("companion", "file.write", payload, mode="companion")
    assert seen and seen[0].rememberable is False  # a "remember" answer creates no grant


def test_the_routes_never_remember_a_write_on_a_node() -> None:
    from types import SimpleNamespace

    from nox.node.install import _install_routes
    from nox.tools.registry import ToolRegistry

    laptop = registry_entry("a" * 32, "Laptop", tools={"file.write": "medium", "shell.run": "high"})
    laptop.confirmer = True
    registry = FakeRegistry([laptop])
    registry.any_online_with = lambda _t: True  # type: ignore[attr-defined]
    core = SimpleNamespace(
        tool_registry=ToolRegistry(),
        config=SimpleNamespace(devices=SimpleNamespace(defaults={})),
    )
    _install_routes(core, registry)  # type: ignore[arg-type]
    write = core.tool_registry.get("file.write")
    shell = core.tool_registry.get("shell.run")
    assert write is not None and shell is not None and write.scope is not None
    payload = write.input_model.model_validate(
        {"path": "a.txt", "text": "x", "device": "Laptop"}
    ).model_dump()
    assert write.scope(payload).rememberable is False
    assert shell.scope is not None
    command = shell.input_model.model_validate({"argv": ["whoami"], "device": "Laptop"})
    assert shell.scope(command.model_dump()).rememberable is True  # a cmd: grant stays possible


def test_a_profile_rule_never_allows_a_node_call_unless_it_names_the_device() -> None:
    from nox.security.model import Decision, PermissionRequest, ProfileRule
    from nox.security.permissions import device_rule_applies

    allow = ProfileRule(id="r", tool="file*", action="write", decision=Decision.ALLOW)
    deny = ProfileRule(id="d", tool="file*", decision=Decision.DENY)
    named = ProfileRule(id="n", tool="file*", decision=Decision.ALLOW, device="a" * 32)
    here = PermissionRequest(agent="a", tool="file", action="write", mode="m", risk=Risk.MEDIUM)
    there = here.model_copy(update={"device": "a" * 32})
    other = here.model_copy(update={"device": "b" * 32})
    assert device_rule_applies(allow, here) and not device_rule_applies(allow, there)
    assert device_rule_applies(deny, there)  # narrowing still applies
    assert device_rule_applies(named, there) and not device_rule_applies(named, other)
    assert not device_rule_applies(named, here)


# ---- profile rules that allow on a node name exactly one device --------------------------------


def test_an_allow_for_a_node_needs_an_exact_device_id() -> None:
    from nox.security.model import Decision, PermissionRequest, Profile, ProfileRule
    from nox.security.permissions import device_rule_applies
    from nox.security.profiles import ProfileError, validate_profile

    for pattern in ("*", "a*", "?" * 32, "A" * 32):
        rule = ProfileRule(id="r", tool="file*", decision=Decision.ALLOW, device=pattern)
        with pytest.raises(ProfileError):
            validate_profile(Profile(id="companion", rules=[rule]))
    exact = ProfileRule(id="e", tool="file*", decision=Decision.ALLOW, device="a" * 32)
    validate_profile(Profile(id="companion", rules=[exact]))
    narrowing = ProfileRule(id="n", tool="file*", decision=Decision.DENY, device="*")
    validate_profile(Profile(id="companion", rules=[narrowing]))
    request = PermissionRequest(
        agent="a", tool="file", action="write", mode="m", risk=Risk.MEDIUM, device="a" * 32
    )
    assert device_rule_applies(exact, request) and device_rule_applies(narrowing, request)
    globbed = ProfileRule(id="g", tool="file*", decision=Decision.ALLOW, device="a*")
    assert not device_rule_applies(globbed, request)  # even if it slipped past validation
