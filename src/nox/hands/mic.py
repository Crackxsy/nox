"""`mic.record_long`: record a longer stretch of speech, but only with stored consent.

For dictation of the user's own voice this is one thing; for a lesson or a meeting it records other
people, which is lawful only with their consent (§ 201 StGB). So the guard is simple and below the
model: the call names a `context`, and unless `ConsentRepository.has_consent(context)` is true the
tool refuses - no entry, no recording. While recording, a visible indicator is on (mic event), and
the raw audio is deleted as soon as it is transcribed (default), so nothing lingers on disk.

The capture + transcription backend is injected (`Transcriber`); this module owns only the consent
gate, the indicator and the delete-after-transcription contract. HIGH risk, CONFIRM, never
rememberable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.hands.consents import ConsentRepository
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = ["Transcriber", "register_mic_tool"]

#: Records for up to `seconds`, transcribes, deletes the raw audio, returns the transcript. The
#: core supplies the real one (local Whisper); a test supplies a fake. It must not persist audio.
Transcriber = Callable[[int], Awaitable[str]]
#: Turns the recording indicator on/off (pet, tray, dashboard). Injected.
Indicator = Callable[[bool], Awaitable[None]]


class MicInput(BaseModel):
    context: str = Field(min_length=1)
    seconds: int = Field(default=60, ge=1, le=3600)


def register_mic_tool(
    registry: ToolRegistry,
    *,
    consents: ConsentRepository,
    transcribe: Transcriber,
    indicator: Indicator | None = None,
) -> None:
    async def record(payload: dict[str, Any]) -> dict[str, Any]:
        model = MicInput.model_validate(payload)
        if not consents.has_consent(model.context):
            log.info("mic.record_refused_no_consent", context=model.context)
            return {
                "ok": False,
                "error": "consent.missing",
                "detail": (
                    f"no stored consent for context {model.context!r}; recording others needs "
                    "their consent first (store it with the consent tool/dashboard)"
                ),
            }
        if indicator is not None:
            await indicator(True)
        try:
            transcript = await transcribe(model.seconds)
        finally:
            if indicator is not None:
                await indicator(False)
        return {"ok": True, "transcript": transcript, "raw_audio_kept": False}

    registry.register(
        ToolSpec(
            name="mic.record_long",
            description="Record and transcribe a longer stretch of speech. Needs stored consent "
            "for the given context when others may be recorded; raw audio is deleted after "
            "transcription.",
            input_model=MicInput,
            risk=Risk.HIGH,
            handler=record,
            side_effects=True,
            local=True,
            rememberable=False,
            targets=lambda p: str(p.get("context", "")),
        )
    )
