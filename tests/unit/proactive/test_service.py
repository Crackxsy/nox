"""ST-19-02/03/04: `ProactiveService.notify()` policy matrix - URGENT ordering, the B.13 mid-match
interrupt exception (safety/data-loss only), the A.13 announce-before-speaking rule, and ordinary
proactive-hint quiet-hours/budget gating."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nox.core.config import NoxConfig
from nox.core.speech_policy import SpeechPolicy
from nox.proactive.service import ProactiveService
from tests.unit.fakes import FakeState


class FakeSpeaker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def say(self, text: str, *, language: str = "de") -> None:
        self.calls.append(text)


def make_service(
    *,
    muted: bool = False,
    active_zone: str | None = None,
    privacy_mode: str = "balanced",
    clock_hour: int = 12,
    config: NoxConfig | None = None,
    speaker: FakeSpeaker | None = None,
) -> tuple[ProactiveService, list[tuple[str, dict[str, Any]]], FakeState]:
    state = FakeState()
    state.state.assistant.muted = muted
    clock_dt = datetime(2026, 9, 14, clock_hour, 0)
    cfg = config or NoxConfig()
    policy = SpeechPolicy(
        state=state,
        config=cfg,
        active_zone=lambda: active_zone,
        privacy_mode=lambda: privacy_mode,
        clock=lambda: clock_dt,
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def publish_event(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    service = ProactiveService(
        state=state,
        config=cfg,
        speech_policy=policy,
        publish_event=publish_event,
        speaker=speaker,
        clock=lambda: clock_dt,
    )
    return service, events, state


# ---- URGENT: security / data-loss - the B.13 mid-match exception ------------------------------


async def test_security_urgent_bypasses_zone_privacy_mode_and_quiet_hours() -> None:
    speaker = FakeSpeaker()
    service, events, _ = make_service(
        active_zone="bedroom", privacy_mode="private", clock_hour=2, speaker=speaker
    )
    decision = await service.notify("urgent", "security", "Kill switch armed")
    assert decision.allowed is True
    assert decision.channel == "speech+toast"
    assert decision.announced is False  # A.13 exemption: URGENT itself is not announced first
    assert speaker.calls == ["Kill switch armed"]
    assert events[0] == (
        "proactive.notification",
        {
            "kind": "urgent",
            "priority": "security",
            "text": "Kill switch armed",
            "channel": "speech+toast",
            "spoken": True,
            "announced": False,
        },
    )


async def test_data_loss_urgent_bypasses_zone_privacy_mode_and_quiet_hours() -> None:
    speaker = FakeSpeaker()
    service, _events, _ = make_service(
        active_zone="bedroom", privacy_mode="offline", clock_hour=3, speaker=speaker
    )
    decision = await service.notify("urgent", "data_loss", "Backup verification failed")
    assert decision.allowed is True
    assert decision.channel == "speech+toast"
    assert speaker.calls == ["Backup verification failed"]


async def test_security_urgent_muted_still_toasts_but_is_never_spoken() -> None:
    """ "Never mute" (epic summary) means the visual channel, not that Klappe-halten is
    overridden."""
    speaker = FakeSpeaker()
    service, events, _ = make_service(muted=True, speaker=speaker)
    decision = await service.notify("urgent", "security", "Kill switch armed")
    assert decision.allowed is True
    assert decision.channel == "toast"
    assert speaker.calls == []
    assert events[0][0] == "proactive.notification"
    assert events[0][1]["spoken"] is False


# ---- URGENT: resources / task_result - "lesser URGENT", a visible warning only ----------------


async def test_lesser_urgent_resources_in_quiet_hours_toasts_without_speaking() -> None:
    speaker = FakeSpeaker()
    service, events, _ = make_service(clock_hour=2, speaker=speaker)  # default quiet hours
    decision = await service.notify("urgent", "resources", "GPU at 95C")
    assert decision.allowed is True
    assert decision.channel == "toast"
    assert speaker.calls == []
    assert events[0][0] == "proactive.notification"


async def test_lesser_urgent_task_result_speaks_and_announces_when_allowed() -> None:
    speaker = FakeSpeaker()
    service, _events, _ = make_service(clock_hour=12, speaker=speaker)
    decision = await service.notify("urgent", "task_result", "Build finished")
    assert decision.allowed is True
    assert decision.channel == "speech+toast"
    assert decision.announced is True
    assert speaker.calls == ["Nox:", "Build finished"]


async def test_lesser_urgent_muted_still_toasts() -> None:
    speaker = FakeSpeaker()
    service, events, _ = make_service(muted=True, speaker=speaker)
    decision = await service.notify("urgent", "resources", "Disk almost full")
    assert decision.allowed is True
    assert decision.channel == "toast"
    assert speaker.calls == []
    assert events[0][1]["spoken"] is False


# ---- ordinary proactive hints -------------------------------------------------------------------


async def test_proactive_hint_suppressed_in_quiet_hours() -> None:
    speaker = FakeSpeaker()
    service, events, _ = make_service(clock_hour=2, speaker=speaker)
    decision = await service.notify("proactive", "normal", "You have 3 new emails")
    assert decision.allowed is False
    assert decision.reason == "quiet_hours"
    assert decision.channel == "suppressed"
    assert speaker.calls == []
    assert events[-1] == (
        "proactive.suppressed",
        {"kind": "proactive", "priority": "normal", "reason": "quiet_hours"},
    )


async def test_proactive_hint_suppressed_in_privacy_zone() -> None:
    service, events, _ = make_service(active_zone="bedroom")
    decision = await service.notify("proactive", "normal", "hint")
    assert decision.allowed is False
    assert decision.reason == "privacy_zone"
    assert events[-1][1]["reason"] == "privacy_zone"


async def test_proactive_hint_suppressed_when_muted() -> None:
    service, _events, _ = make_service(muted=True)
    decision = await service.notify("proactive", "normal", "hint")
    assert decision.allowed is False
    assert decision.reason == "muted"


async def test_proactive_hint_announces_before_speaking() -> None:
    speaker = FakeSpeaker()
    service, _events, _ = make_service(speaker=speaker)
    decision = await service.notify("proactive", "low", "Coffee's ready")
    assert decision.allowed is True
    assert decision.announced is True
    assert speaker.calls == ["Nox:", "Coffee's ready"]


async def test_proactive_hint_suppressed_in_focus_mode() -> None:
    cfg = NoxConfig()
    cfg.attention.per_mode = {"focus": 0}
    speaker = FakeSpeaker()
    service, events, state = make_service(config=cfg, speaker=speaker)
    state.state.assistant.mode = "focus"  # type: ignore[assignment]
    decision = await service.notify("proactive", "normal", "hint")
    assert decision.allowed is False
    assert decision.reason == "focus_mode"
    assert speaker.calls == []
    assert events[-1][1]["reason"] == "focus_mode"


async def test_proactive_hint_suppressed_once_interruption_budget_is_used_up() -> None:
    cfg = NoxConfig()
    cfg.attention.interruptions_per_hour = 1
    speaker = FakeSpeaker()
    service, events, _ = make_service(config=cfg, speaker=speaker)
    first = await service.notify("proactive", "normal", "hint one")
    assert first.allowed is True
    second = await service.notify("proactive", "normal", "hint two")
    assert second.allowed is False
    assert second.reason == "interruption_budget"
    assert speaker.calls == ["Nox:", "hint one"]
    assert events[-1][1]["reason"] == "interruption_budget"


async def test_urgent_never_consumes_the_interruption_budget() -> None:
    cfg = NoxConfig()
    cfg.attention.interruptions_per_hour = 1
    speaker = FakeSpeaker()
    service, _events, _ = make_service(config=cfg, speaker=speaker)
    await service.notify("urgent", "security", "warning one")
    await service.notify("urgent", "security", "warning two")
    # the ordinary hint budget (1/hour) is untouched by URGENT calls
    hint = await service.notify("proactive", "normal", "hint")
    assert hint.allowed is True


async def test_status_reports_focus_mode_and_budget() -> None:
    cfg = NoxConfig()
    cfg.attention.interruptions_per_hour = 5
    service, _events, _ = make_service(config=cfg)
    status = service.status()
    assert status.enabled is True
    assert status.focus_mode is False
    assert status.interruptions_budget == 5
    assert status.interruptions_used_this_hour == 0


async def test_recent_notifications_are_stored() -> None:
    speaker = FakeSpeaker()
    service, _events, _ = make_service(speaker=speaker)
    await service.notify("urgent", "security", "warning")
    status = service.status()
    assert len(status.recent) == 1
    assert status.recent[0].text == "warning"
    assert status.recent[0].kind == "urgent"
