"""Negative tests for the browser and senses permission controls.

Each one fails if the control it covers is removed: screen capture in PRIVATE/OFFLINE or under a
privacy zone, browser tools in PRIVATE/OFFLINE, a page action without confirmation, a remembered
click that leaks to another origin or another action, and a remembered click honoured after
untrusted browser content entered the turn.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nox.core.state import PrivacyMode
from nox.security.model import Decision, PermissionRequest, Profile, Risk, TemporaryGrant
from nox.security.permissions import (
    DefaultPermissionEngine,
    InMemoryGrantStore,
    PrivacySnapshot,
    grant_matches,
)

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
SHOP = "origin:https://shop.example"


class _Privacy:
    def __init__(self, snapshot: PrivacySnapshot) -> None:
        self.value = snapshot

    def snapshot(self) -> PrivacySnapshot:
        return self.value


class _Profiles:
    def get(self, _id: str) -> Profile:
        return Profile(id="companion", rules=[])


def _engine(**snapshot: object) -> tuple[DefaultPermissionEngine, _Privacy]:
    privacy = _Privacy(PrivacySnapshot.model_validate(snapshot))
    engine = DefaultPermissionEngine(
        profiles=_Profiles(),  # type: ignore[arg-type]
        privacy=privacy,
        grants=InMemoryGrantStore(),
        clock=lambda: NOW,
    )
    return engine, privacy


def _req(tool: str, action: str, risk: Risk, target: str = "", **kw: object) -> PermissionRequest:
    return PermissionRequest.model_validate(
        {
            "agent": "companion",
            "tool": tool,
            "action": action,
            "mode": "companion",
            "risk": risk,
            "target": target,
            **kw,
        }
    )


SCREEN = ("screen", "capture", Risk.MEDIUM, "screen")
CAMERA = ("camera", "snapshot", Risk.MEDIUM, "camera")


# ---- screen / camera / vision ----------------------------------------------------------------


@pytest.mark.parametrize("mode", [PrivacyMode.PRIVATE, PrivacyMode.OFFLINE])
def test_screen_capture_is_denied_in_private_and_offline(mode: PrivacyMode) -> None:
    engine, _ = _engine(mode=mode)
    result = engine.check(_req(*SCREEN))
    assert result.decision is Decision.DENY
    assert result.rule_id == f"privacy.{mode.value}.screen"


def test_screen_capture_in_private_is_denied_even_with_a_remembered_grant() -> None:
    engine, _ = _engine(mode=PrivacyMode.PRIVATE)
    engine.grant_temporary(
        TemporaryGrant(
            grant_id="g",
            scope="session:screen/capture",
            max_risk=Risk.MEDIUM,
            expires_at=NOW + timedelta(hours=1),
        )
    )
    assert engine.check(_req(*SCREEN)).decision is Decision.DENY


def test_screen_capture_asks_in_balanced_and_is_denied_under_a_zone() -> None:
    engine, privacy = _engine(mode=PrivacyMode.BALANCED)
    assert engine.check(_req(*SCREEN)).decision is Decision.CONFIRM
    privacy.value = PrivacySnapshot(mode=PrivacyMode.FULL, zone_active=True)
    result = engine.check(_req(*SCREEN))
    assert result.decision is Decision.DENY and result.rule_id == "privacy.zone_active"


def test_camera_always_asks_and_a_zone_denies_it() -> None:
    engine, privacy = _engine(mode=PrivacyMode.FULL)
    assert engine.check(_req(*CAMERA)).decision is Decision.CONFIRM
    privacy.value = PrivacySnapshot(mode=PrivacyMode.FULL, zone_active=True)
    assert engine.check(_req(*CAMERA)).decision is Decision.DENY


def test_camera_snapshot_is_never_remembered() -> None:
    engine, _ = _engine()

    async def scenario() -> None:
        grant_id = engine.request_confirmation(_req(*CAMERA, rememberable=False))
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.decision is Decision.ALLOW and result.grant_id is None
        assert engine.check(_req(*CAMERA, rememberable=False)).decision is Decision.CONFIRM

    asyncio.run(scenario())


def test_vision_is_local_capable_so_private_does_not_deny_it_but_a_zone_does() -> None:
    engine, privacy = _engine(mode=PrivacyMode.PRIVATE)
    assert engine.check(_req("vision", "describe", Risk.LOW, "x.png")).decision is Decision.ALLOW
    privacy.value = PrivacySnapshot(mode=PrivacyMode.FULL, zone_active=True)
    assert engine.check(_req("vision", "describe", Risk.LOW, "x.png")).decision is Decision.DENY


# ---- browser ---------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [PrivacyMode.PRIVATE, PrivacyMode.OFFLINE])
@pytest.mark.parametrize("action", ["open", "read", "screenshot", "click"])
def test_browser_is_a_cloud_tool(mode: PrivacyMode, action: str) -> None:
    engine, _ = _engine(mode=mode)
    result = engine.check(_req("browser", action, Risk.LOW, "https://example.com/"))
    assert result.decision is Decision.DENY
    assert result.rule_id == f"privacy.{mode.value}.cloud"


def test_browser_reading_is_allowed_and_acting_asks() -> None:
    engine, _ = _engine(mode=PrivacyMode.BALANCED)
    assert engine.check(_req("browser", "open", Risk.LOW, "https://a.example/")).decision is (
        Decision.ALLOW
    )
    for action in ("click", "type", "submit"):
        assert engine.check(_req("browser", action, Risk.MEDIUM, SHOP)).decision is (
            Decision.CONFIRM
        )


def test_remembering_a_click_covers_that_action_on_that_origin_only() -> None:
    engine, _ = _engine()

    async def scenario() -> None:
        grant_id = engine.request_confirmation(_req("browser", "click", Risk.MEDIUM, SHOP))
        engine.reply(grant_id, Decision.ALLOW, remember=True)
        result = await engine.await_confirmation(grant_id)
        assert result.grant_id is not None
        assert engine.check(_req("browser", "click", Risk.MEDIUM, SHOP)).decision is (
            Decision.ALLOW
        )
        other_site = _req("browser", "click", Risk.MEDIUM, "origin:https://bank.example")
        assert engine.check(other_site).decision is Decision.CONFIRM
        other_action = _req("browser", "submit", Risk.MEDIUM, SHOP)
        assert engine.check(other_action).decision is Decision.CONFIRM
        # Another scheme or port is another origin.
        assert engine.check(
            _req("browser", "click", Risk.MEDIUM, "origin:http://shop.example")
        ).decision is (Decision.CONFIRM)

    asyncio.run(scenario())


def test_a_browser_target_without_an_origin_is_never_remembered() -> None:
    engine, _ = _engine()

    async def scenario() -> None:
        for target in ("origin:invalid", "https://shop.example/", ""):
            grant_id = engine.request_confirmation(_req("browser", "click", Risk.MEDIUM, target))
            engine.reply(grant_id, Decision.ALLOW, remember=True)
            assert (await engine.await_confirmation(grant_id)).grant_id is None

    asyncio.run(scenario())


def test_a_remembered_click_asks_again_after_browser_content_tainted_the_turn() -> None:
    engine, _ = _engine()
    engine.grant_temporary(
        TemporaryGrant(
            grant_id="g",
            scope="site:click@https://shop.example",
            max_risk=Risk.MEDIUM,
            expires_at=NOW + timedelta(hours=1),
        )
    )
    click = _req("browser", "click", Risk.MEDIUM, SHOP, conversation="chat:1")
    assert engine.check(click).decision is Decision.ALLOW
    engine.mark_tainted("chat:1")  # browser.read put untrusted page text into the conversation
    assert engine.check(click).decision is Decision.CONFIRM
    # Another conversation's taint does not touch this one, and vice versa.
    other = _req("browser", "click", Risk.MEDIUM, SHOP, conversation="voice:2")
    assert engine.check(other).decision is Decision.ALLOW


@pytest.mark.parametrize(
    ("scope", "tool", "action", "target", "expected"),
    [
        ("site:click@https://a.example", "browser", "click", "origin:https://a.example", True),
        ("site:click@https://a.example", "browser", "click", "origin:https://b.example", False),
        ("site:click@https://a.example", "browser", "type", "origin:https://a.example", False),
        ("site:click@https://a.example", "shell", "click", "origin:https://a.example", False),
        ("site:click@https://a.example", "browser", "click", "https://a.example", False),
        ("site:@https://a.example", "browser", "", "origin:https://a.example", False),
        ("site:click@", "browser", "click", "origin:", False),
    ],
)
def test_site_grant_grammar(
    scope: str, tool: str, action: str, target: str, expected: bool
) -> None:
    grant = TemporaryGrant(
        grant_id="g", scope=scope, max_risk=Risk.MEDIUM, expires_at=NOW + timedelta(hours=1)
    )
    request = _req(tool, action, Risk.MEDIUM, target)
    assert grant_matches(grant, request, NOW) is expected
