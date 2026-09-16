"""nox.tools.registry: register/get/names/unregister/describe, duplicate rejection."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec


class EchoInput(BaseModel):
    text: str = ""


async def _echo(arguments: dict[str, Any]) -> dict[str, Any]:
    return {"text": arguments.get("text", "")}


def make_spec(name: str = "echo.say", *, risk: Risk = Risk.LOW, local: bool = True) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="Echoes text back.",
        input_model=EchoInput,
        risk=risk,
        handler=_echo,
        local=local,
    )


def test_register_get_names_unregister(registry: ToolRegistry) -> None:
    spec = make_spec()
    registry.register(spec)
    assert registry.get("echo.say") is spec
    assert registry.names() == ["echo.say"]
    assert "echo.say" in registry
    assert len(registry) == 1

    registry.unregister("echo.say")
    assert registry.get("echo.say") is None
    assert registry.names() == []
    assert len(registry) == 0


def test_unregister_unknown_is_a_noop(registry: ToolRegistry) -> None:
    registry.unregister("no.such.tool")  # must not raise


def test_register_duplicate_raises(registry: ToolRegistry) -> None:
    registry.register(make_spec())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_spec())


def test_describe_has_no_handlers_and_is_sorted(registry: ToolRegistry) -> None:
    registry.register(make_spec("zzz.tool"))
    registry.register(make_spec("aaa.tool", risk=Risk.READ, local=False))
    described = registry.describe()
    assert [d.name for d in described] == ["aaa.tool", "zzz.tool"]
    for entry in described:
        assert not hasattr(entry, "handler")
    aaa = described[0]
    assert aaa.risk is Risk.READ
    assert aaa.local is False
    assert aaa.input_schema["title"] == "EchoInput"
