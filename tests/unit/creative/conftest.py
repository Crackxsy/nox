"""Fakes for `nox.creative` core-side service tests: no real `NoxCore`, no real IPC, no real
`SecurityContext` - just the small surface `CreativeModeService`/`CreativeScreenshotService`
actually use (`_CoreLike` protocols in `service.py`/`screenshot.py`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nox.core.state import PrivacyMode
from tests.unit.fakes import FakeBus


class FakeProfile:
    def __init__(self, profile_id: str) -> None:
        self.id = profile_id


class FakeEngine:
    def __init__(self, profile_id: str = "companion") -> None:
        self._profile = FakeProfile(profile_id)

    def active_profile(self) -> FakeProfile:
        return self._profile


class FakePrivacy:
    def __init__(
        self,
        mode: PrivacyMode = PrivacyMode.BALANCED,
        *,
        zone: str | None = None,
        capture_screen: bool = True,
    ) -> None:
        self.mode = mode
        self._zone = zone
        self._capture_screen = capture_screen

    def zone_active(self, window_title: str = "", process_name: str = "") -> bool:
        return self._zone is not None

    def allows_capture(self, kind: str) -> bool:
        return self._capture_screen if kind == "screen" else True


class FakeSecurity:
    def __init__(
        self, engine: FakeEngine | None = None, privacy: FakePrivacy | None = None
    ) -> None:
        self.engine = engine or FakeEngine()
        self.privacy = privacy or FakePrivacy()


class FakePaths:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir


class FakeConfig:
    def __init__(self, runtime_dir: Path) -> None:
        self.paths = FakePaths(runtime_dir)


class FakeState:
    def __init__(self, mode: str = "companion", *, mode_locked: bool = False) -> None:
        self._values: dict[str, Any] = {
            "assistant.mode": mode,
            "assistant.mode_locked": mode_locked,
        }
        self.updates: list[tuple[str, Any]] = []

    def get(self, path: str) -> Any:
        return self._values.get(path)

    async def update(self, path: str, value: Any, *, reason: str = "") -> int:
        self._values[path] = value
        self.updates.append((path, value))
        return len(self.updates)


class FakeCore:
    def __init__(
        self,
        *,
        mode: str = "companion",
        mode_locked: bool = False,
        profile_id: str = "companion",
        privacy: FakePrivacy | None = None,
        runtime_dir: Path | None = None,
    ) -> None:
        self.bus = FakeBus()
        self.state = FakeState(mode, mode_locked=mode_locked)
        self.security = FakeSecurity(FakeEngine(profile_id), privacy)
        self.config = FakeConfig(runtime_dir or Path("."))
