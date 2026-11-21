"""The loudness of what Nox is saying, published while it plays (`tts.level`).

Kept apart from the pipeline because it is a loop of its own with one job: read the output
device's block loudness a few dozen times a second and report the changes. It never sees the words.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any

from nox.core.events import E
from nox.voice._logging import get_logger
from nox.voice.base import AudioOutput, TtsRequest

#: How often the loudness of what is playing is published (`tts.level`), per second.
LEVEL_HZ = 30
#: Smallest change in loudness worth an event.
LEVEL_STEP = 0.01

log = get_logger(__name__)


async def pace_levels(
    audio_out: AudioOutput,
    emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    request: TtsRequest,
) -> None:
    """While an utterance plays: its loudness as `tts.level`, about LEVEL_HZ times a second.

    The level is measured where the audio leaves (the output device's block RMS), so the
    pet's mouth moves with what is heard, not with synthesis running ahead of playback. Only
    changes are sent; silence costs one event, not thirty a second. Never the words.
    """
    last = -1.0
    while True:
        raw = float(getattr(audio_out, "level", 0.0) or 0.0)
        level = round(min(1.0, max(0.0, raw)), 3) if math.isfinite(raw) else 0.0
        if abs(level - last) >= LEVEL_STEP or (level == 0.0 and last != 0.0):
            last = level
            try:
                await emit(
                    E.TTS_LEVEL,
                    {
                        "utterance_id": request.utterance_id,
                        "channel": str(request.channel),
                        "level": level,
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a lost level must never stop speech
                log.debug("voice.tts_level_dropped", error=str(exc))
        await asyncio.sleep(1.0 / LEVEL_HZ)
