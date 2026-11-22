"""The input side of the voice pipeline, assembled from the configuration.

`DefaultVoicePipeline` takes a ready `Segmenter`; this builds it around the speech detector
(`nox.voice.vad_silero`, with the energy detector behind it). Keeping the assembly here leaves the
pipeline free of detector details, and gives the worker one object to ask for the health note
about them.
"""

from __future__ import annotations

from dataclasses import dataclass

from nox.core.config.assistant import VoiceConfig
from nox.core.events import HealthStatus
from nox.voice.models import engine_models_dir
from nox.voice.vad import Segmenter
from nox.voice.vad_silero import ResilientVad, SileroParams, build_vad


@dataclass(slots=True)
class CaptureChain:
    """What the worker keeps of the input side besides the segmenter itself."""

    segmenter: Segmenter
    vad: ResilientVad

    def notes(self) -> str:
        """Health notes about the input side, '' when nothing needs the user's attention.

        Reported next to the microphone state: today, that the neural detector could not be used
        and the energy detector (which cannot tell speech from other loud sound) is in its place.
        """
        status, text = self.vad.health()
        return "" if status is HealthStatus.AVAILABLE else text


def build_capture_chain(
    voice: VoiceConfig,
    *,
    sample_rate: int,
    end_silence_ms: int,
    max_utterance_ms: int,
    min_utterance_ms: int,
    frame_ms: int = 30,
) -> CaptureChain:
    """Build the segmenter for a pipeline from the `voice` configuration."""
    vad = build_vad(
        engine=voice.vad.engine,
        configured_model=voice.vad.model,
        models_dir=engine_models_dir("silero", voice.models_dir),
        params=SileroParams(
            threshold=voice.vad.threshold,
            threshold_off=voice.vad.threshold_off,
            budget_ms=voice.vad.budget_ms,
        ),
    )
    segmenter = Segmenter(
        vad=vad,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
        end_silence_ms=end_silence_ms,
        max_segment_ms=max_utterance_ms,
        min_segment_ms=min_utterance_ms,
    )
    return CaptureChain(segmenter=segmenter, vad=vad)
