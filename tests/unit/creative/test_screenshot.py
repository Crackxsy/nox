"""`CreativeScreenshotService.decide_and_capture` (Spec v0.7 §3.2/§5, ST-16-05): the consent-gate
and privacy-zone-refusal logic the `creative` plugin cannot implement itself."""

from __future__ import annotations

from pathlib import Path

from nox.core.events import E
from nox.core.state import PrivacyMode
from nox.creative.screenshot import CreativeScreenshotService

from .conftest import FakeCore, FakePrivacy


class TestDecideAndCapture:
    def test_refuses_in_private(self) -> None:
        core = FakeCore(privacy=FakePrivacy(PrivacyMode.PRIVATE))
        service = CreativeScreenshotService(core)
        status, reason, path = service.decide_and_capture("blender.exe", "Untitled.blend")
        assert status == "refused"
        assert "private" in reason.lower()
        assert path == ""

    def test_refuses_in_offline(self) -> None:
        core = FakeCore(privacy=FakePrivacy(PrivacyMode.OFFLINE))
        service = CreativeScreenshotService(core)
        status, _reason, _path = service.decide_and_capture("blender.exe", "Untitled.blend")
        assert status == "refused"

    def test_refuses_inside_a_privacy_zone(self) -> None:
        core = FakeCore(privacy=FakePrivacy(PrivacyMode.FULL, zone="private_chats"))
        service = CreativeScreenshotService(core)
        status, reason, _path = service.decide_and_capture("blender.exe", "Untitled.blend")
        assert status == "refused"
        assert "zone" in reason.lower()

    def test_refuses_under_work_profile(self) -> None:
        core = FakeCore(profile_id="work", privacy=FakePrivacy(PrivacyMode.FULL))
        service = CreativeScreenshotService(core)
        status, reason, _path = service.decide_and_capture("blender.exe", "Untitled.blend")
        assert status == "refused"
        assert "work" in reason.lower()

    def test_refuses_when_capture_disabled(self) -> None:
        core = FakeCore(privacy=FakePrivacy(PrivacyMode.FULL, capture_screen=False))
        service = CreativeScreenshotService(core)
        status, _reason, _path = service.decide_and_capture("blender.exe", "Untitled.blend")
        assert status == "refused"

    def test_allowed_but_mss_not_installed_is_honestly_unavailable(self, monkeypatch) -> None:
        import nox.creative.screenshot as mod

        monkeypatch.setattr(mod, "mss_available", lambda: False)
        core = FakeCore(privacy=FakePrivacy(PrivacyMode.BALANCED))
        service = CreativeScreenshotService(core)
        status, reason, path = service.decide_and_capture("blender.exe", "Untitled.blend")
        assert status == "unavailable"
        assert "mss" in reason
        assert path == ""

    def test_captures_when_allowed_and_mss_available(self, tmp_path: Path, monkeypatch) -> None:
        import nox.creative.screenshot as mod

        monkeypatch.setattr(mod, "mss_available", lambda: True)
        monkeypatch.setattr(
            CreativeScreenshotService, "_capture", lambda self, app: str(tmp_path / "shot.png")
        )
        core = FakeCore(privacy=FakePrivacy(PrivacyMode.FULL), runtime_dir=tmp_path)
        service = CreativeScreenshotService(core)
        status, reason, path = service.decide_and_capture("blender.exe", "Untitled.blend")
        assert status == "captured"
        assert reason == ""
        assert path == str(tmp_path / "shot.png")

    def test_capture_failure_is_honestly_unavailable(self, monkeypatch) -> None:
        import nox.creative.screenshot as mod

        monkeypatch.setattr(mod, "mss_available", lambda: True)

        def _boom(self: CreativeScreenshotService, app: str) -> str:
            raise RuntimeError("no display")

        monkeypatch.setattr(CreativeScreenshotService, "_capture", _boom)
        core = FakeCore(privacy=FakePrivacy(PrivacyMode.FULL))
        service = CreativeScreenshotService(core)
        status, reason, _path = service.decide_and_capture("blender.exe", "Untitled.blend")
        assert status == "unavailable"
        assert "no display" in reason


class TestEventRoundTrip:
    async def test_request_event_publishes_result_event(self, monkeypatch) -> None:
        import nox.creative.screenshot as mod

        monkeypatch.setattr(mod, "mss_available", lambda: False)
        core = FakeCore(privacy=FakePrivacy(PrivacyMode.FULL))
        service = CreativeScreenshotService(core)
        service.start()

        await core.bus.fire(
            E.CREATIVE_SCREENSHOT_REQUESTED,
            {"corr": "abc123", "app": "blender.exe", "window_title": "Untitled.blend"},
        )

        result = core.bus.published[-1]
        assert result.name == E.CREATIVE_SCREENSHOT_RESULT
        assert result.payload["corr"] == "abc123"
        assert result.payload["status"] == "unavailable"


class TestProfileGateFailsClosed:
    """A security engine that cannot answer must never be read as "no Work profile"."""

    def test_a_failing_profile_lookup_refuses_the_capture(self, monkeypatch) -> None:
        core = FakeCore(privacy=FakePrivacy(PrivacyMode.FULL))

        def _boom() -> object:
            raise RuntimeError("security engine not ready")

        monkeypatch.setattr(core.security.engine, "active_profile", _boom)
        service = CreativeScreenshotService(core)

        status, reason, path = service.decide_and_capture("blender.exe", "Untitled.blend")

        assert status == "unavailable"
        assert "could not be read" in reason
        assert path == ""
