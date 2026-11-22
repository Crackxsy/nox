"""The input side of the voice pipeline, assembled from the configuration.

`DefaultVoicePipeline` takes a ready `Segmenter`; this builds it: the speech detector
(`nox.voice.vad_silero`, with the energy detector behind it), wrapped in the echo rule
(`nox.voice.echo`) so that Nox's own voice from loudspeakers does not count as the user's. Keeping
the assembly here leaves the pipeline free of detector and echo details, and gives the worker one
object to ask for the health note about them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from nox.core.config.assistant import VoiceConfig
from nox.core.events import HealthStatus
from nox.voice.echo import EchoGuard, EchoGuardedVad, EchoMode, EchoParams, OutputProbe
from nox.voice.models import engine_models_dir
from nox.voice.vad import Segmenter
from nox.voice.vad_silero import ResilientVad, SileroParams, build_vad


@dataclass(slots=True)
class CaptureChain:
    """What the worker keeps of the input side besides the segmenter itself."""

    segmenter: Segmenter
    vad: ResilientVad
    guard: EchoGuard

    def bind_speaking(self, speaking: Callable[[], bool]) -> None:
        """Tell the echo rule when Nox is speaking (the pipeline exists only after the chain)."""
        self.guard.bind_speaking(speaking)

    def notes(self) -> str:
        """Health notes about the input side, '' when nothing needs the user's attention.

        Reported next to the microphone state: the energy fallback (the neural detector could not
        be used) and, for loudspeakers, the headphones recommendation.
        """
        notes: list[str] = []
        status, text = self.vad.health()
        if status is not HealthStatus.AVAILABLE:
            notes.append(text)
        echo = self.guard.hint()
        if echo:
            notes.append(echo)
        return "; ".join(notes)


def build_capture_chain(
    voice: VoiceConfig,
    *,
    sample_rate: int,
    end_silence_ms: int,
    max_utterance_ms: int,
    min_utterance_ms: int,
    frame_ms: int = 30,
    probe: OutputProbe | None = None,
    spawn: Callable[[Callable[[], None]], None] | None = None,
) -> CaptureChain:
    """Build the segmenter for a pipeline from `voice` and start looking at the output device."""
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
    guard = EchoGuard(
        params=EchoParams(
            mode=EchoMode(voice.echo.mode),
            start_db=voice.echo.start_db,
            min_ms=voice.echo.min_ms,
            tail_ms=voice.echo.tail_ms,
        ),
        probe=probe,
        spawn=spawn,
        device=voice.channels.private_device,
        # Only `private` and `both` play on this machine; `stream` goes to a cable, `mute` nowhere.
        listens_here=voice.channels.routing in ("private", "both"),
    )
    segmenter = Segmenter(
        vad=EchoGuardedVad(vad, guard, sample_rate=sample_rate),
        sample_rate=sample_rate,
        frame_ms=frame_ms,
        end_silence_ms=end_silence_ms,
        max_segment_ms=max_utterance_ms,
        min_segment_ms=min_utterance_ms,
    )
    guard.refresh_now()
    return CaptureChain(segmenter=segmenter, vad=vad, guard=guard)
