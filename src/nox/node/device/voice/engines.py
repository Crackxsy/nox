"""The core's voice engines (`nox.worker.main`) loaded on this device, off the event loop, and the
callables the satellite uses to ask and tell the core."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nox.node.device.voice.settings import VoiceSettings, listening_mode, voice_config


@dataclass
class VoiceEngines:
    audio_in: Any
    audio_out: Any
    stt: Any
    tts: Any
    wake_gate: Any = None

    async def unload(self) -> None:
        for engine in (self.stt, self.tts):
            with contextlib.suppress(Exception):
                await engine.unload()


async def load_engines(settings: VoiceSettings, home: Path) -> VoiceEngines:
    """The core's engines on this device (`nox.worker.main`), loaded off the event loop."""
    from nox.worker.main import build_components, build_wake_gate, load_tts  # noqa: PLC0415

    cfg = voice_config(settings, home)
    parts = await asyncio.to_thread(build_components, cfg, service="voice")
    stt, tts = parts["stt"], parts["tts"]
    # The engine never downloads by itself: the models come from `nox node voice setup`.
    await stt.load()
    tts = await load_tts(tts, cfg)
    gate = build_wake_gate(cfg) if listening_mode(settings) == "wake_word" else None
    return VoiceEngines(parts["audio_in"], parts["audio_out"], stt, tts, gate)


EngineLoader = Callable[[VoiceSettings, Path], Awaitable[VoiceEngines]]


#: Sends one request to the core and returns its answer (None: not connected / no answer).
AskCore = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


#: Sends one notification to the core.
TellCore = Callable[[str, dict[str, Any]], Awaitable[None]]
