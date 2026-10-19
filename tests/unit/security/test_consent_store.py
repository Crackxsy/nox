"""Review finding 14: there must be a way to STORE a consent - and the model must not be able to
store one by itself. The tool always asks and is never remembered; the dashboard needs the PIN while
one is configured; every row says who agreed, the wording, when, and how it was recorded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nox.data.db import Database
from nox.hands.consent_tools import (
    ConsentGrant,
    ConsentRevoke,
    register_consent_ipc,
    register_consent_tools,
)
from nox.hands.consents import ConsentRepository
from nox.ipc.dispatch import EmptyPayload
from nox.ipc.errors import IpcError
from nox.security.gate import PinRequiredError
from nox.security.model import Decision, PermissionRequest, Risk
from nox.security.permissions import DefaultPermissionEngine
from nox.tools.registry import ToolRegistry


@pytest.fixture
def consents(tmp_path: Path) -> Iterator[ConsentRepository]:
    db = Database(tmp_path / "nox.db")
    db.migrate()
    yield ConsentRepository(db)
    db.close()


class _Gate:
    def __init__(self, *, required: bool, pin: str = "4711") -> None:
        self.required = required
        self.pin = pin

    def is_required(self) -> bool:
        return self.required

    async def require(self, pin: str | None, *, action: str, by: str) -> None:
        if pin != self.pin:
            raise PinRequiredError(action, "the PIN is required for this change")


class _Ipc:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.roles: dict[str, tuple[str, ...]] = {}

    def register(self, name: str, model: Any, handler: Any, *, roles: tuple[str, ...]) -> None:
        self.handlers[name] = handler
        self.roles[name] = roles


class _Ctx:
    role = "dashboard"


def test_the_grant_tool_always_asks_and_is_never_remembered(
    consents: ConsentRepository, engine: DefaultPermissionEngine
) -> None:
    registry = ToolRegistry()
    register_consent_tools(registry, consents)
    spec = registry.get("consent.grant")
    assert spec is not None
    assert spec.risk is Risk.HIGH and spec.rememberable is False
    target = spec.targets({"context": "lesson:math", "who": "class 10b", "wording": "ok to record"})  # type: ignore[misc]
    assert "class 10b" in target and "ok to record" in target  # the user sees what is stored
    request = PermissionRequest(
        agent="companion",
        tool="consent",
        action="grant",
        mode="companion",
        risk=spec.risk,
        target=target,
        rememberable=spec.rememberable,
    )
    assert engine.check(request).decision is Decision.CONFIRM

    async def confirm_with_remember() -> None:
        grant_id = engine.request_confirmation(request)
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        await engine.await_confirmation(grant_id)

    asyncio.run(confirm_with_remember())
    # "Remember" produced nothing: the next grant asks again.
    assert engine.check(request).decision is Decision.CONFIRM


def test_tool_grant_records_who_what_when_and_how(consents: ConsentRepository) -> None:
    registry = ToolRegistry()
    register_consent_tools(registry, consents)
    handler = registry.get("consent.grant").handler  # type: ignore[union-attr]
    result = asyncio.run(
        handler({"context": "lesson:math", "who": "class 10b", "wording": "recording ok"})
    )
    row = result["consent"]
    assert row["who"] == "class 10b" and row["wording"] == "recording ok"
    assert row["granted_at"] and row["recorded_by"] == "conversation, confirmed by the user"
    assert consents.has_consent("lesson:math")


def test_dashboard_grant_needs_the_pin_when_one_is_set(consents: ConsentRepository) -> None:
    ipc = _Ipc()
    register_consent_ipc(ipc, consents, _Gate(required=True))
    assert ipc.roles["consent.grant"] == ("shell", "dashboard")
    grant = ipc.handlers["consent.grant"]
    payload = ConsentGrant(context="lesson:math", who="class", wording="ok")
    with pytest.raises(IpcError):
        asyncio.run(grant(_Ctx(), payload))
    assert not consents.has_consent("lesson:math")
    payload_with_pin = ConsentGrant(context="lesson:math", who="class", wording="ok", pin="4711")
    result = asyncio.run(grant(_Ctx(), payload_with_pin))
    assert result["consent"]["recorded_by"] == "dashboard (dashboard)"
    assert consents.has_consent("lesson:math")


def test_dashboard_list_and_revoke(consents: ConsentRepository) -> None:
    ipc = _Ipc()
    register_consent_ipc(ipc, consents, _Gate(required=False))
    asyncio.run(
        ipc.handlers["consent.grant"](
            _Ctx(), ConsentGrant(context="meeting:team", who="team", wording="ok")
        )
    )
    listed = asyncio.run(ipc.handlers["consent.list"](_Ctx(), EmptyPayload()))
    assert [c["context"] for c in listed["consents"]] == ["meeting:team"]
    revoked = asyncio.run(
        ipc.handlers["consent.revoke"](_Ctx(), ConsentRevoke(context="meeting:team"))
    )
    assert revoked["revoked"] == 1
    assert not consents.has_consent("meeting:team")
