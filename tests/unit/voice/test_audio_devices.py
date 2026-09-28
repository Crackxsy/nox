"""SoundDeviceOutput with a fake sounddevice: a device that cannot be opened is reported as
unavailable and dropped from the device cache (no hardware, no PortAudio)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

import nox.voice.audio as audio
from nox.voice.audio import AudioUnavailableError, SoundDeviceOutput
from nox.voice.base import Channel


async def _pcm() -> AsyncIterator[bytes]:
    yield b"\x00\x00" * 100


async def test_an_output_device_that_vanished_is_forgotten_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unplugged(**_kwargs: Any) -> Any:
        raise OSError("PortAudio: device unavailable")

    monkeypatch.setattr(audio, "_sd", lambda: SimpleNamespace(OutputStream=unplugged))
    out = SoundDeviceOutput()
    out._resolved["headset"] = 7  # an index cached before the headset was unplugged
    with pytest.raises(AudioUnavailableError, match="cannot open the output device"):
        await out.play(_pcm(), 22050, Channel.PRIVATE, utterance_id="u1")
    assert out._resolved == {}
