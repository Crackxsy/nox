"""Shared helpers for the voice-satellite tests: a scripted microphone, speaker, speech
recognition and voice, a satellite built from them, and the core's voice hub with a fake
node session. No audio device is ever opened."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from nox.ipc.tokens import TokenStore
from nox.node.device.gate import NodeGate
from nox.node.device.voice import VoiceEngines, VoiceSatellite, VoiceSettings
from nox.node.hub.satellites import NodeDirectory, OverlayAccess, SatelliteHub, VoiceAssistant
from nox.node.wire import protocol as p
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.node.wire.receipts import ReceiptSigner, ReceiptVerifier

PI = "a" * 32


PC = "b" * 32


# ---- fakes ---------------------------------------------------------------------------------------


class FakeIn:
    """A microphone that is only ever marked open; no device is touched."""

    sample_rate = 16000

    def __init__(self) -> None:
        self.open = False
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def frames(self) -> AsyncIterator[np.ndarray]:
        await asyncio.Event().wait()  # no microphone: frames never come
        yield np.zeros(160, dtype=np.int16)

    @property
    def enabled(self) -> bool:
        return self.open

    async def set_enabled(self, value: bool) -> None:
        self.open = value


class FakeOut:
    """A speaker that records what was played."""

    level = 0.0

    def __init__(self) -> None:
        self.played: list[tuple[str, int]] = []
        self.channels: list[Any] = []

    async def play(self, chunks: Any, rate: int, channel: Any, *, utterance_id: str) -> None:
        size = 0
        async for chunk in chunks:
            size += len(chunk)
        self.played.append((utterance_id, size))
        self.channels.append(channel)

    async def stop(self, *, reason: str) -> None:
        return None

    def devices(self) -> list[dict[str, str]]:
        return []


class FakeStt:
    """Speech recognition that loads and unloads; the tests feed text in themselves."""

    id = "fake-stt"

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        return None


class FakeTts:
    """A voice that records what it was asked to say."""

    id = "supertonic"
    sample_rate = 16000

    def __init__(self) -> None:
        self.said: list[str] = []
        self.unloaded = False

    async def load(self) -> None:
        return None

    async def unload(self) -> None:
        self.unloaded = True

    async def synthesize(self, request: Any) -> AsyncIterator[bytes]:
        self.said.append(request.text)
        yield b"\x00\x00" * 32


def make_satellite(
    tmp_path: Path, *, allowed: bool = True, answer: dict[str, Any] | None = None
) -> tuple[VoiceSatellite, dict[str, Any]]:
    """A voice satellite over the fakes, and everything it asked, told, stopped and audited."""
    seen: dict[str, Any] = {"asked": [], "told": [], "stops": [], "audit": [], "loads": 0}
    engines = VoiceEngines(FakeIn(), FakeOut(), FakeStt(), FakeTts())
    seen["engines"] = engines
    flags = {"allowed": allowed}
    seen["flags"] = flags

    async def load(_settings: VoiceSettings, _home: Path) -> VoiceEngines:
        seen["loads"] += 1
        return engines

    async def ask(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        seen["asked"].append((method, params))
        return answer if answer is not None else {"result": {"accepted": True}}

    async def tell(method: str, params: dict[str, Any]) -> None:
        seen["told"].append((method, params))

    satellite = VoiceSatellite(
        home=tmp_path,
        settings=VoiceSettings,
        allowed=lambda: flags["allowed"],
        ask_core=ask,
        tell_core=tell,
        stop_here=seen["stops"].append,
        audit=lambda action, result: seen["audit"].append((action, result)),
        load=load,
    )
    return satellite, seen


async def running_satellite(tmp_path: Path, **kw: Any) -> tuple[VoiceSatellite, dict[str, Any]]:
    """A satellite that is connected, allowed to capture and refreshed."""
    satellite, seen = make_satellite(tmp_path, **kw)
    satellite.connection(True)
    await satellite.on_gate({"capture": True})
    await satellite.refresh()
    return satellite, seen


def satellite_gate(
    tools: dict[str, Any], groups: tuple[str, ...], keys: Path
) -> tuple[NodeGate, Any]:
    """The node's gate over `tools` with only `groups` switched on, and its signer."""
    signer = ReceiptSigner(keys / "receipts")
    manifest = NodeManifest(groups={g: g in groups for g in GROUPS})
    gate = NodeGate(tools, manifest, None)
    gate.receipts = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    return gate, signer


# ---- the core: voice turns ----------------------------------------------------------------------


class FakeClient:
    """The core's end of a node connection: records the notifications sent to the node."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.sent.append((method, params))


def fake_session(device_id: str = PI) -> Any:
    """A ready listener session of the device with a `FakeClient`."""
    ready = asyncio.Event()
    ready.set()
    return SimpleNamespace(device_id=device_id, client=FakeClient(), ready=ready)


def voice_hub(
    *,
    offers: set[tuple[str, str]] | None = None,
    capture: str | None = None,
    turn: Any = None,
    sessions: list[Any] | None = None,
    overlay: Any = None,
    disconnected: list[list[str]] | None = None,
    opted_in: bool = True,
    may_hold: set[str] | None = None,
) -> tuple[SatelliteHub, dict[str, Any]]:
    """The core's `SatelliteHub` with scripted offers, turns and privacy, and what it did."""
    seen: dict[str, Any] = {"published": [], "turns": [], "audit": [], "controlled": False}
    allowed = offers if offers is not None else {(PI, "voice.say"), (PI, "obs.request")}

    async def default_turn(
        text: str, language: str, on_chunk: Any, channel: str, note: str
    ) -> None:
        from nox.memory.capture import _TURN_ORIGIN

        seen["turns"].append((text, language, channel, _TURN_ORIGIN.get()))
        seen["notes"] = [*seen.get("notes", []), note]
        for part in ("Es ist ", "zwölf Uhr. ", "Noch etwas?"):
            await on_chunk(part)

    async def disconnect(ids: list[str]) -> int:
        if disconnected is not None:
            disconnected.append(ids)
        return len(ids)

    audit = SimpleNamespace(append=lambda **kw: seen["audit"].append(kw))
    hub = SatelliteHub(
        NodeDirectory(
            offers=lambda device, tool: (device, tool) in allowed,
            enabled_on_device=lambda device, group: (
                group == "obs" and (device, "obs.request") in allowed
            ),
            sessions=lambda: sessions or [],
            opted_in=lambda _device, _group: opted_in,
            label=lambda _device: "Living room Pi",
        ),
        VoiceAssistant(
            turn=turn or default_turn,
            capture_refusal=lambda: capture,
            publish=lambda name, payload: seen["published"].append((name, payload)),
        ),
        OverlayAccess(
            store=overlay,
            disconnect=disconnect,
            allowed=lambda device: device in (may_hold if may_hold is not None else {PI}),
        ),
        audit=audit,  # type: ignore[arg-type]
    )
    hub.controlled = lambda _device: seen["controlled"]
    return hub, seen


def heard(text: str = "Wie spät ist es?") -> p.VoiceHeard:
    """What a node sends when it heard a sentence."""
    return p.VoiceHeard(turn_id="e" * 32, text=text, language="de")


def lan_accepts(tokens: TokenStore, token: str, role: str = "overlay") -> bool:
    """Whether the token store accepts `token` for `role` from the home network."""
    return tokens.authenticate(token, role, f"{role}:x", via_lan=True).ok
