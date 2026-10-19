"""screen.capture / camera.snapshot / vision.describe against fake grabbers and models.

No real screen, camera or model is touched. The negative tests fail if their control is removed:
a capture in PRIVATE or under a privacy zone, a camera picture without the capture flag, a camera
or screen image sent to the cloud where privacy forbids it, a file outside the captures folder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nox.core.state import PrivacyMode
from nox.hands import senses as senses_mod
from nox.hands.senses import CaptureUnavailable, Senses, cloud_refusal, register_sense_tools
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakePrivacy:
    def __init__(
        self,
        mode: PrivacyMode = PrivacyMode.BALANCED,
        *,
        zone: str | None = None,
        screen: bool = True,
        camera: bool = False,
        panic: bool = False,
    ) -> None:
        self.mode = mode
        self.active_zone = zone
        self.flags = {"screen": screen, "camera": camera, "microphone": True}
        self.panic = panic

    def allows_capture(self, kind: str) -> bool:
        return self.flags[kind] and not self.panic and self.active_zone is None

    def allows_cloud(self) -> bool:
        return self.mode in (PrivacyMode.FULL, PrivacyMode.BALANCED) and not self.panic

    def allows_screenshot_to_cloud(self) -> bool:
        return self.mode is PrivacyMode.FULL and self.allows_cloud() and self.active_zone is None


class Recorder:
    def __init__(self, result: bytes = PNG, error: Exception | None = None) -> None:
        self.calls = 0
        self.result = result
        self.error = error

    async def __call__(self) -> bytes:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class FakeModel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.images: list[bytes] = []
        self.prompts: list[str] = []

    async def describe(self, png: bytes, prompt: str) -> str:
        self.images.append(png)
        self.prompts.append(prompt)
        return f"described by {self.name}"


def _senses(tmp_path: Path, privacy: FakePrivacy, **kw: object) -> Senses:
    return Senses(captures_root=tmp_path / "captures", privacy=privacy, **kw)  # type: ignore[arg-type]


# ---- screen.capture --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [PrivacyMode.PRIVATE, PrivacyMode.OFFLINE])
async def test_screen_capture_refused_in_private_and_offline(tmp_path: Path, mode) -> None:  # noqa: ANN001
    grab = Recorder()
    result = await _senses(tmp_path, FakePrivacy(mode), screen=grab).capture_screen()
    assert result["ok"] is False and result["error"] == "capture.refused"
    assert grab.calls == 0


async def test_screen_capture_refused_under_a_privacy_zone(tmp_path: Path) -> None:
    grab = Recorder()
    result = await _senses(tmp_path, FakePrivacy(zone="banking"), screen=grab).capture_screen()
    assert result["error"] == "capture.refused" and "zone" in result["detail"]
    assert grab.calls == 0


async def test_screen_capture_refused_when_the_flag_is_off_or_panic(tmp_path: Path) -> None:
    for privacy in (FakePrivacy(screen=False), FakePrivacy(panic=True)):
        grab = Recorder()
        result = await _senses(tmp_path, privacy, screen=grab).capture_screen()
        assert result["error"] == "capture.refused" and grab.calls == 0


async def test_screen_capture_stores_locally_and_returns_only_a_path(tmp_path: Path) -> None:
    flags: list[tuple[str, bool]] = []

    async def indicator(kind: str, active: bool) -> None:
        flags.append((kind, active))

    senses = _senses(tmp_path, FakePrivacy(), screen=Recorder(), indicator=indicator)
    result = await senses.capture_screen()
    assert result["ok"] is True and result["stays_local"] is True
    path = Path(result["path"])
    assert path.parent == tmp_path / "captures" / "screen"
    assert path.read_bytes() == PNG  # noqa: ASYNC240 - a test reading a file
    assert PNG.decode("latin-1") not in str(result)  # the image itself is never in the result
    assert result["cloud_allowed"] is False  # BALANCED: a screen capture stays local
    assert flags == [("screen", True), ("screen", False)]
    full = await _senses(
        tmp_path, FakePrivacy(PrivacyMode.FULL), screen=Recorder()
    ).capture_screen()
    assert full["cloud_allowed"] is True


async def test_unavailable_screen_is_reported_not_faked(tmp_path: Path) -> None:
    grab = Recorder(error=CaptureUnavailable("no display: this machine runs headless"))
    result = await _senses(tmp_path, FakePrivacy(), screen=grab).capture_screen()
    assert result == {
        "ok": False,
        "error": "capture.unavailable",
        "detail": "no display: this machine runs headless",
    }


def test_headless_linux_has_no_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert "headless" in (senses_mod.screen_support() or "")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert "Wayland" in (senses_mod.screen_support() or "")


# ---- camera.snapshot -------------------------------------------------------------------------


async def test_camera_refused_while_the_camera_flag_is_off(tmp_path: Path) -> None:
    grab = Recorder()
    result = await _senses(tmp_path, FakePrivacy(camera=False), camera=grab).snapshot_camera()
    assert result["error"] == "capture.refused" and grab.calls == 0


async def test_camera_snapshot_shows_the_indicator_and_stays_local(tmp_path: Path) -> None:
    flags: list[tuple[str, bool]] = []

    async def indicator(kind: str, active: bool) -> None:
        flags.append((kind, active))

    grab = Recorder()
    senses = _senses(tmp_path, FakePrivacy(camera=True), camera=grab, indicator=indicator)
    result = await senses.snapshot_camera()
    assert result["ok"] is True and result["cloud_allowed"] is False
    assert flags == [("camera", True), ("camera", False)]
    assert set(result) == {"ok", "path", "stays_local", "cloud_allowed"}  # no embeddings, no ids


async def test_the_camera_indicator_turns_off_even_when_capture_fails(tmp_path: Path) -> None:
    flags: list[tuple[str, bool]] = []

    async def indicator(kind: str, active: bool) -> None:
        flags.append((kind, active))

    grab = Recorder(error=CaptureUnavailable("no camera found"))
    senses = _senses(tmp_path, FakePrivacy(camera=True), camera=grab, indicator=indicator)
    assert (await senses.snapshot_camera())["error"] == "capture.unavailable"
    assert flags == [("camera", True), ("camera", False)]


# ---- vision.describe -------------------------------------------------------------------------


async def _capture(tmp_path: Path, kind: str) -> str:
    folder = tmp_path / "captures" / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{kind}-1-abc.png"
    path.write_bytes(PNG)
    return str(path)


async def test_describe_refuses_a_file_that_is_not_a_capture(tmp_path: Path) -> None:
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    await _capture(tmp_path, "screen")
    cloud = FakeModel("claude")
    senses = _senses(tmp_path, FakePrivacy(PrivacyMode.FULL), cloud_vision=cloud.describe)
    for raw in (
        str(secret),
        str(tmp_path / "captures" / "screen" / ".." / ".." / "secret.png"),
        str(tmp_path / "captures" / "screen-1.png"),
        str(tmp_path / "captures" / "other" / "x.png"),
    ):
        result = await senses.describe(raw, "")
        assert result["error"] == "vision.not_a_capture", raw
    assert cloud.images == []


async def test_a_local_model_is_used_first_even_when_the_cloud_is_allowed(tmp_path: Path) -> None:
    path = await _capture(tmp_path, "browser")
    local, cloud = FakeModel("llava"), FakeModel("claude")

    async def model() -> str | None:
        return "llava"

    senses = _senses(
        tmp_path,
        FakePrivacy(PrivacyMode.FULL),
        local_vision=local.describe,
        local_model=model,
        cloud_vision=cloud.describe,
    )
    result = await senses.describe(path, "what is this?")
    assert result["processed"] == "local" and result["model"] == "llava"
    assert cloud.images == []
    assert "Do not identify" in local.prompts[0]


@pytest.mark.parametrize(
    ("kind", "privacy", "allowed"),
    [
        ("screen", FakePrivacy(PrivacyMode.FULL), True),
        ("screen", FakePrivacy(PrivacyMode.BALANCED), False),
        ("screen", FakePrivacy(PrivacyMode.FULL, zone="banking"), False),
        ("browser", FakePrivacy(PrivacyMode.BALANCED), True),
        ("browser", FakePrivacy(PrivacyMode.BALANCED, zone="email"), False),
        ("browser", FakePrivacy(PrivacyMode.PRIVATE), False),
        ("browser", FakePrivacy(PrivacyMode.OFFLINE), False),
        ("browser", FakePrivacy(PrivacyMode.FULL, panic=True), False),
        ("camera", FakePrivacy(PrivacyMode.FULL, camera=True), False),
    ],
)
async def test_where_an_image_may_go_without_a_local_model(
    tmp_path: Path, kind: str, privacy: FakePrivacy, allowed: bool
) -> None:
    path = await _capture(tmp_path, kind)
    cloud = FakeModel("claude")

    async def no_model() -> str | None:
        return None

    senses = _senses(tmp_path, privacy, local_model=no_model, cloud_vision=cloud.describe)
    result = await senses.describe(path, "")
    assert (cloud_refusal(kind, privacy) is None) is allowed
    if allowed:
        assert result["processed"] == "cloud" and "Anthropic" in result["image_sent_to"]
        assert cloud.images == [PNG]
    else:
        assert result["error"] == "vision.unavailable"
        assert cloud.images == [], "the image must not leave the machine"


async def test_a_failing_local_model_falls_back_only_where_the_cloud_is_allowed(
    tmp_path: Path,
) -> None:
    path = await _capture(tmp_path, "camera")
    cloud = FakeModel("claude")

    async def broken(_png: bytes, _prompt: str) -> str:
        raise CaptureUnavailable("model unloaded")

    async def model() -> str | None:
        return "llava"

    senses = _senses(
        tmp_path,
        FakePrivacy(PrivacyMode.FULL, camera=True),
        local_vision=broken,
        local_model=model,
        cloud_vision=cloud.describe,
    )
    assert (await senses.describe(path, ""))["error"] == "vision.unavailable"
    assert cloud.images == []


def test_registration_risks_and_rememberability(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_sense_tools(registry, _senses(tmp_path, FakePrivacy()))
    screen, camera, vision = (
        registry.get(n) for n in ("screen.capture", "camera.snapshot", "vision.describe")
    )
    assert screen is not None and screen.risk is Risk.MEDIUM and screen.rememberable
    assert camera is not None and camera.risk is Risk.MEDIUM and not camera.rememberable
    assert vision is not None and vision.risk is Risk.LOW


async def test_old_captures_are_pruned(tmp_path: Path) -> None:
    senses = _senses(tmp_path, FakePrivacy(), screen=Recorder(), keep=2)
    paths = []
    for i in range(4):
        result = await senses.capture_screen()
        paths.append(Path(result["path"]))
        os.utime(paths[-1], (1000 + i, 1000 + i))
    left = sorted((tmp_path / "captures" / "screen").glob("*.png"))
    assert len(left) == 2 and paths[-1] in left
