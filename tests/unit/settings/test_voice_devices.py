"""Choosing Nox's audio output devices: the list offered in Settings, the choice validation, and
the live switch through the voice worker. No real audio device is opened."""

from __future__ import annotations

from typing import Any

import pytest

from nox.core.config import NoxConfig
from nox.settings import devices
from nox.settings.devices import OUTPUT_DEVICES, OutputDeviceCatalog, device_choices
from nox.settings.install import build_appliers
from nox.settings.schema import InvalidChoiceError, check_choice, describe
from nox.voice.base import Channel

CABLE = "CABLE Input (VB-Audio Virtual Cable)"
HEADSET = "Headphones (USB Audio)"


@pytest.fixture(autouse=True)
def _reset_catalog() -> Any:
    saved = OUTPUT_DEVICES.names()
    yield
    OUTPUT_DEVICES.set(saved)


async def test_catalog_prefers_the_voice_worker() -> None:
    catalog = OutputDeviceCatalog()

    async def ask(name: str, _payload: dict[str, Any]) -> dict[str, Any]:
        assert name == "voice.devices.list"
        return {"outputs": [HEADSET, CABLE, CABLE]}

    assert await catalog.refresh(ask) == [CABLE, HEADSET]


async def test_catalog_falls_back_to_this_process_and_keeps_the_last_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = OutputDeviceCatalog()

    async def broken(_name: str, _payload: dict[str, Any]) -> dict[str, Any]:
        raise TimeoutError

    monkeypatch.setattr(devices, "_local_output_devices", lambda: [HEADSET])
    assert await catalog.refresh(broken) == [HEADSET]
    monkeypatch.setattr(devices, "_local_output_devices", lambda: None)
    assert await catalog.refresh(None) == [HEADSET]


def test_choices_offer_the_default_every_device_and_the_current_value() -> None:
    OUTPUT_DEVICES.set([CABLE, HEADSET])
    assert device_choices("") == ["", CABLE, HEADSET]
    assert device_choices("Speakers") == ["", CABLE, HEADSET, "Speakers"]
    OUTPUT_DEVICES.set(None)
    assert device_choices("x") is None


def test_device_settings_are_a_pick_list_and_refuse_unknown_devices() -> None:
    OUTPUT_DEVICES.set([CABLE, HEADSET])
    spec = describe("voice.channels.stream_device", NoxConfig())
    assert spec.type == "enum" and spec.options == ["", CABLE, HEADSET]
    assert spec.restart_required is False
    check_choice(spec, CABLE)
    with pytest.raises(InvalidChoiceError):
        check_choice(spec, "Microphone (Webcam)")


class _Hub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(
        self, client_id: str, name: str, payload: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        self.requests.append((client_id, name, payload))
        return {"ok": True}


class _Workers:
    def client_id(self, service: str) -> str | None:
        return "worker:voice" if service == "voice" else None


class _Orchestrator:
    class config:  # noqa: N801 - mirrors the attribute the applier writes
        channel = Channel.PRIVATE


class _Core:
    def __init__(self) -> None:
        self.config = NoxConfig()
        self.orchestrator = _Orchestrator()
        self.security = None
        self.hub = _Hub()
        self.workers = _Workers()


async def test_device_change_reaches_the_voice_worker_live() -> None:
    core = _Core()
    appliers = build_appliers(core)
    await appliers["voice.channels.stream_device"](CABLE)
    assert core.config.voice.channels.stream_device == CABLE
    assert core.hub.requests == [
        (
            "worker:voice",
            "voice.devices.set",
            {"private_device": "", "stream_device": CABLE},
        )
    ]


async def test_routing_change_reaches_the_orchestrator_live() -> None:
    core = _Core()
    await build_appliers(core)["voice.channels.routing"]("both")
    assert core.orchestrator.config.channel is Channel.BOTH


async def test_without_a_voice_worker_the_change_is_only_saved() -> None:
    core = _Core()
    core.workers = None  # type: ignore[assignment]
    await build_appliers(core)["voice.channels.private_device"](HEADSET)
    assert core.config.voice.channels.private_device == HEADSET
    assert core.hub.requests == []
