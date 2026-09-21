"""`CreativeModeService`: maps `creative.app_detected`/`creative.app_left` onto
`assistant.mode` + `system.mode_changed`, mirroring `NoxCore._h_mode_set` (Spec v0.7 §3.1 step 3/4,
acceptance criteria in §8)."""

from __future__ import annotations

from nox.core.events import E
from nox.core.state import Mode
from nox.creative.service import CreativeModeService

from .conftest import FakeCore


class TestCreativeModeService:
    async def test_detected_switches_to_creative(self) -> None:
        core = FakeCore(mode="companion")
        service = CreativeModeService(core)
        service.start()

        await core.bus.fire(E.CREATIVE_APP_DETECTED, {"app": "blender", "window_title": "x"})

        assert core.state.get("assistant.mode") == Mode.CREATIVE.value
        assert core.bus.published[-1].name == E.SYSTEM_MODE_CHANGED
        assert core.bus.published[-1].payload["current"] == Mode.CREATIVE.value

    async def test_left_reverts_to_companion(self) -> None:
        core = FakeCore(mode="companion")
        service = CreativeModeService(core)
        service.start()
        await core.bus.fire(E.CREATIVE_APP_DETECTED, {"app": "blender", "window_title": "x"})

        await core.bus.fire(E.CREATIVE_APP_LEFT, {"app": "blender"})

        assert core.state.get("assistant.mode") == Mode.COMPANION.value

    async def test_left_does_not_touch_a_different_current_mode(self) -> None:
        """If something else changed the mode away from creative in the meantime, `app_left` must
        not clobber it (Spec v0.7 §3.1 step 4: "unchanged existing mode-arbitration logic")."""
        core = FakeCore(mode="research")
        service = CreativeModeService(core)
        service.start()

        await core.bus.fire(E.CREATIVE_APP_LEFT, {"app": "blender"})

        assert core.state.get("assistant.mode") == "research"
        assert core.state.updates == []
        assert not any(e.name == E.SYSTEM_MODE_CHANGED for e in core.bus.published)

    async def test_work_profile_suppresses_the_switch(self) -> None:
        core = FakeCore(mode="companion", profile_id="work")
        service = CreativeModeService(core)
        service.start()

        await core.bus.fire(E.CREATIVE_APP_DETECTED, {"app": "blender", "window_title": "x"})

        assert core.state.get("assistant.mode") == "companion"
        assert core.state.updates == []
        assert not any(e.name == E.SYSTEM_MODE_CHANGED for e in core.bus.published)

    async def test_mode_locked_suppresses_the_switch(self) -> None:
        core = FakeCore(mode="companion", mode_locked=True)
        service = CreativeModeService(core)
        service.start()

        await core.bus.fire(E.CREATIVE_APP_DETECTED, {"app": "blender", "window_title": "x"})

        assert core.state.get("assistant.mode") == "companion"

    async def test_already_creative_does_not_republish(self) -> None:
        core = FakeCore(mode="creative")
        service = CreativeModeService(core)
        service.start()

        await core.bus.fire(E.CREATIVE_APP_DETECTED, {"app": "blender", "window_title": "x"})

        assert core.state.updates == []
        assert not any(e.name == E.SYSTEM_MODE_CHANGED for e in core.bus.published)

    async def test_stop_unsubscribes(self) -> None:
        core = FakeCore(mode="companion")
        service = CreativeModeService(core)
        service.start()
        service.stop()

        await core.bus.fire(E.CREATIVE_APP_DETECTED, {"app": "blender", "window_title": "x"})

        assert core.state.get("assistant.mode") == "companion"


async def test_a_failing_profile_lookup_suppresses_the_mode_switch(monkeypatch) -> None:
    """Without a readable profile we cannot know the Work profile is off, so we do not switch."""
    core = FakeCore()

    def _boom() -> object:
        raise RuntimeError("security engine not ready")

    monkeypatch.setattr(core.security.engine, "active_profile", _boom)
    service = CreativeModeService(core)
    service.start()

    await core.bus.fire(E.CREATIVE_APP_DETECTED, {"app": "blender.exe", "window_title": "x"})

    assert core.state.get("assistant.mode") == "companion"
    assert E.SYSTEM_MODE_CHANGED not in [e.name for e in core.bus.published]
