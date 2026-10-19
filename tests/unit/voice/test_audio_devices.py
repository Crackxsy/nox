"""Device selection in `nox.voice.audio` against a fake `sounddevice`: names, indices, the list
offered to the user and the live switch. Nothing is opened or played."""

from __future__ import annotations

from typing import Any

import pytest

from nox.voice import audio
from nox.voice.audio import (
    AudioUnavailableError,
    SoundDeviceOutput,
    output_device_names,
    resolve_device,
)


def _dev(name: str, hostapi: int, inputs: int, outputs: int) -> dict[str, Any]:
    return {
        "name": name,
        "hostapi": hostapi,
        "max_input_channels": inputs,
        "max_output_channels": outputs,
        "default_samplerate": 48000.0,
    }


MME, WASAPI = 0, 1
DEVICES = [
    _dev("Microphone (Webcam)", MME, 1, 0),
    _dev("CABLE Input (VB-Audio Virtual C", MME, 0, 2),  # MME cuts names at 31 characters
    _dev("Headphones (USB Audio)", MME, 0, 2),
    _dev("CABLE Input (VB-Audio Virtual Cable)", WASAPI, 0, 2),
    _dev("Headphones (USB Audio)", WASAPI, 0, 2),
]


class _FakeSd:
    default = type("D", (), {"device": (0, 2)})()

    @staticmethod
    def query_hostapis() -> list[dict[str, Any]]:
        return [{"name": "MME"}, {"name": "Windows WASAPI"}]

    @staticmethod
    def query_devices() -> list[dict[str, Any]]:
        return DEVICES


@pytest.fixture(autouse=True)
def fake_sd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio, "_sd", lambda: _FakeSd)


def test_names_prefer_wasapi_full_names() -> None:
    assert output_device_names() == [
        "CABLE Input (VB-Audio Virtual Cable)",
        "Headphones (USB Audio)",
    ]


def test_a_name_resolves_to_the_wasapi_device() -> None:
    assert resolve_device("cable input", kind="output") == 3
    assert resolve_device("", kind="output") is None


@pytest.mark.parametrize("setting", ["#2", "2", " #2 "])
def test_an_index_names_the_device(setting: str) -> None:
    assert resolve_device(setting, kind="output") == 2


@pytest.mark.parametrize("setting", ["#0", "#99"])
def test_an_index_without_output_or_out_of_range_is_refused(setting: str) -> None:
    with pytest.raises(AudioUnavailableError):
        resolve_device(setting, kind="output")


async def test_set_devices_switches_for_the_next_resolution() -> None:
    out = SoundDeviceOutput(private_device="", stream_device="")
    assert await out._resolve("Headphones") == 4
    out.set_devices(private_device="#2", stream_device="CABLE")
    assert out.device_names == ("#2", "CABLE")
    assert out._resolved == {}  # cache dropped: the next utterance resolves the new devices
    assert await out._targets(audio.Channel.BOTH) == [2, 3]
