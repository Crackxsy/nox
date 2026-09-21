"""The `home.*` IPC surface: who may call it, and what `home.command` does with one sentence.

Two things are pinned here. Only the two user-facing roles may reach these requests - never a
plugin, a worker or the paired phone - and `home.command` runs the deterministic layer *before*
anything else, reports which of match/refusal/no-match happened, and executes only on a match.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nox.core.config import HomeConfig
from nox.home.ipc import UI_ROLES, register_home_ipc
from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.errors import ERR_PERMISSION, ERR_UNAVAILABLE, IpcError
from nox.ipc.protocol import NAME_ERROR, Envelope, Kind, Source
from nox.tools.executor import ERR_PERMISSION_DENIED, ERR_UNKNOWN_TOOL, ToolResult

HOUSE_LISTING: dict[str, Any] = {
    "ok": True,
    "connected": True,
    "reason": "",
    "areas": ["Wohnzimmer", "Küche"],
    "areas_available": True,
    "entities": [
        {"entity_id": "light.wz_decke", "name": "Deckenlampe", "area": "Wohnzimmer", "state": "on"},
        {"entity_id": "light.wz_steh", "name": "Stehlampe", "area": "Wohnzimmer", "state": "on"},
        {"entity_id": "switch.kaffee", "name": "Kaffeemaschine", "area": "Küche", "state": "off"},
    ],
}


class FakeExecutor:
    """Records every tool call and answers from a scripted table."""

    def __init__(self, results: dict[str, ToolResult] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results = results or {}

    async def call(
        self, *, agent: str, name: str, arguments: dict[str, Any], mode: str, **_: Any
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if name in self.results:
            return self.results[name]
        if name == "home.list":
            return ToolResult(ok=True, data=HOUSE_LISTING, decision="allow")
        return ToolResult(ok=True, data={"ok": True, "connected": True}, decision="allow")

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _registry(
    executor: FakeExecutor,
    *,
    token: str | None = "t",
    probe: httpx.Response | None = None,
) -> RequestRegistry:
    registry = RequestRegistry()

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return probe or httpx.Response(200, json={"message": "API running."})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    register_home_ipc(
        registry,
        executor,  # type: ignore[arg-type]
        lambda: "companion",
        settings=lambda: HomeConfig(),
        token=lambda: token,
        client_factory=client_factory,
    )
    return registry


def _context(role: str = "dashboard") -> RequestContext:
    return RequestContext(
        client_id=f"{role}:1",
        role=role,
        request=Envelope(kind=Kind.REQUEST, name="home.list", src=Source(role=role, id="1")),
    )


async def _call(registry: RequestRegistry, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    envelope = Envelope(
        kind=Kind.REQUEST, name=name, src=Source(role="dashboard", id="1"), payload=payload
    )
    reply = await registry.dispatch(_context(), envelope)
    if reply.name == NAME_ERROR:
        raise IpcError(str(reply.payload["code"]), str(reply.payload.get("message", "")))
    return dict(reply.payload)


# ---- registration and roles -----------------------------------------------------------------


def test_the_seven_requests_are_registered_for_the_two_ui_roles_only() -> None:
    registry = _registry(FakeExecutor())
    expected = [
        "home.command",
        "home.light",
        "home.list",
        "home.scene",
        "home.status",
        "home.switch",
        "home.test",
    ]
    assert registry.names() == expected
    for name in expected:
        for role in UI_ROLES:
            assert registry.is_allowed(name, role) is True
        for role in ("plugin", "worker", "pet", "remote", "supervisor"):
            assert registry.is_allowed(name, role) is False


async def test_a_denied_tool_call_becomes_a_permission_error_not_a_crash() -> None:
    executor = FakeExecutor(
        {"home.light": ToolResult(ok=False, error=ERR_PERMISSION_DENIED, decision="deny")}
    )
    registry = _registry(executor)
    with pytest.raises(IpcError) as excinfo:
        await _call(registry, "home.light", {"entity_ids": ["light.wz_decke"], "on": True})
    assert excinfo.value.code == ERR_PERMISSION


async def test_a_plugin_that_is_not_enabled_says_so_instead_of_failing_internally() -> None:
    """The ordinary state of a fresh installation, and the one the Zuhause page has to explain."""
    executor = FakeExecutor(
        {"home.list": ToolResult(ok=False, error=ERR_UNKNOWN_TOOL, decision="deny")}
    )
    with pytest.raises(IpcError) as excinfo:
        await _call(_registry(executor), "home.list", {})
    assert excinfo.value.code == ERR_UNAVAILABLE
    assert "plugins.enabled" in str(excinfo.value)


async def test_list_is_forwarded_to_the_tool_unchanged() -> None:
    executor = FakeExecutor()
    result = await _call(_registry(executor), "home.list", {"area": "Wohnzimmer"})
    assert executor.calls == [("home.list", {"area": "Wohnzimmer", "domain": ""})]
    assert result["entities"][0]["entity_id"] == "light.wz_decke"


# ---- home.command ---------------------------------------------------------------------------


async def test_a_matching_sentence_is_executed_without_touching_the_model() -> None:
    executor = FakeExecutor()
    result = await _call(
        _registry(executor), "home.command", {"text": "mach das licht im wohnzimmer aus"}
    )
    assert result["matched"] is True
    assert result["tool"] == "home.light"
    assert result["summary"].startswith("Licht Wohnzimmer")
    assert executor.names() == ["home.list", "home.light"]
    assert executor.calls[1][1] == {
        "entity_ids": ["light.wz_decke", "light.wz_steh"],
        "on": False,
    }
    assert result["match_ms"] >= 0.0


async def test_a_sentence_about_a_lock_is_refused_and_nothing_is_called() -> None:
    executor = FakeExecutor()
    result = await _call(_registry(executor), "home.command", {"text": "schließ die haustür auf"})
    assert result["matched"] is False
    assert result["refused"] is True
    assert "hard boundary" in result["reason"]
    assert executor.names() == ["home.list"]


async def test_a_sentence_it_does_not_understand_reports_no_match_for_the_caller_to_fall_back() -> (
    None
):
    executor = FakeExecutor()
    result = await _call(_registry(executor), "home.command", {"text": "erzähl mir einen witz"})
    assert result["matched"] is False
    assert result["refused"] is False
    assert result["tool"] == ""
    assert executor.names() == ["home.list"]


# ---- home.test ------------------------------------------------------------------------------


async def test_the_connection_test_reports_the_real_result() -> None:
    result = await _call(_registry(FakeExecutor()), "home.test", {})
    assert result["ok"] is True
    assert result["code"] == "ok"


async def test_the_connection_test_says_when_there_is_no_token() -> None:
    result = await _call(_registry(FakeExecutor(), token=None), "home.test", {})
    assert result["ok"] is False
    assert result["code"] == "no_token"
