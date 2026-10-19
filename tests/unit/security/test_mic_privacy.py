"""Review finding 14: `mic.record_long` must obey privacy zones and the microphone capture switch.

`CAPTURE_TOOL_PATTERNS` listed "microphone" but the tool is called `mic`, so a privacy zone did not
stop it, and the `privacy.capture.microphone` switch was not consulted by the engine at all.
"""

from __future__ import annotations

from nox.security.model import Decision, PermissionRequest, Risk
from nox.security.permissions import DefaultPermissionEngine
from nox.security.privacy import PrivacyService


def _mic() -> PermissionRequest:
    return PermissionRequest(
        agent="companion",
        tool="mic",
        action="record_long",
        mode="companion",
        risk=Risk.HIGH,
        target="lesson:math",
    )


async def test_mic_is_refused_while_a_privacy_zone_is_active(
    engine: DefaultPermissionEngine, privacy: PrivacyService
) -> None:
    assert engine.check(_mic()).decision is Decision.CONFIRM
    zone = await privacy.observe_foreground("Online-Banking - Sparkasse", "chrome.exe")
    assert zone is not None
    result = engine.check(_mic())
    assert result.decision is Decision.DENY
    assert result.rule_id == "privacy.zone_active"


async def test_mic_is_refused_when_the_microphone_switch_is_off(
    engine: DefaultPermissionEngine, privacy: PrivacyService
) -> None:
    assert engine.check(_mic()).decision is Decision.CONFIRM
    await privacy.set_capture(microphone=False, by="test")
    result = engine.check(_mic())
    assert result.decision is Decision.DENY
    assert result.rule_id == "privacy.capture.microphone"
    await privacy.set_capture(microphone=True, by="test")
    assert engine.check(_mic()).decision is Decision.CONFIRM


async def test_camera_switch_off_by_default_refuses_camera_tools(
    engine: DefaultPermissionEngine,
) -> None:
    camera = PermissionRequest(
        agent="companion", tool="camera", action="snapshot", mode="companion", risk=Risk.HIGH
    )
    # The fixture's privacy has camera capture off, as the shipped defaults do.
    assert engine.check(camera).decision is Decision.DENY
