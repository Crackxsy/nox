"""Piper as the safety net behind every other TTS engine.

When the configured engine cannot load (model files not downloaded, optional extra not installed,
an unknown voice name), the voice worker speaks with Piper instead of going silent. The swap is
never hidden: `health` stays `limited` and names the engine that was wanted and why it is missing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from nox.core.events import HealthStatus
from nox.voice.base import TtsRequest


class FallbackTts:
    """Delegates to `engine` and reports that it stands in for `wanted`."""

    def __init__(self, engine: Any, *, wanted: str, reason: str) -> None:
        self.engine = engine
        self.wanted = wanted
        self.reason = reason
        self.id = engine.id

    @property
    def sample_rate(self) -> int:
        return int(self.engine.sample_rate)

    @property
    def loaded_voices(self) -> list[str]:
        return list(self.engine.loaded_voices)

    @property
    def load_time_ms(self) -> float:
        return float(self.engine.load_time_ms)

    async def health(self) -> tuple[HealthStatus, str]:
        status, detail = await self.engine.health()
        if status is HealthStatus.UNAVAILABLE:
            return status, detail
        return HealthStatus.LIMITED, (
            f"{self.wanted} unavailable ({self.reason}); speaking with {self.engine.id} instead"
        )

    async def load(self) -> None:
        await self.engine.load()

    def synthesize(self, request: TtsRequest) -> AsyncIterator[bytes]:
        result: AsyncIterator[bytes] = self.engine.synthesize(request)
        return result

    async def unload(self) -> None:
        await self.engine.unload()
