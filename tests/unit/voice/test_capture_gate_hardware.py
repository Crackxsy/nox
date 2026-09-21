"""The capture gate as a hardware property: `ptt_only` keeps the capture device closed.

`listening_mode: ptt_only` is documented as "the microphone is not open unless push-to-talk is
held". These tests protect that as a property of the device, not of the frame filter: the pipeline
must not ask the input to open before push-to-talk, and must close it again on release.
"""

from __future__ import annotations

from typing import Any

from nox.core.events import E, HealthStatus
from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig

from .conftest import FakeAudioInput, FakeAudioOutput, FakeStt, FakeTts, settle


def make_pipeline(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput, **config: Any
) -> DefaultVoicePipeline:
    async def emit(_name: str, _payload: dict[str, Any]) -> None:
        return None

    return DefaultVoicePipeline(
        audio_in=audio_in,
        audio_out=audio_out,
        stt=FakeStt(),
        tts=FakeTts(),
        emit=emit,
        capture_allowed=lambda: True,
        config=PipelineConfig(**config),
    )


async def test_ptt_only_never_opens_the_capture_device_before_push_to_talk(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    pipeline = make_pipeline(audio_in, audio_out, listening_mode="ptt_only")
    await pipeline.start()
    assert audio_in.enabled is False
    assert audio_in.opens == 0  # no device was ever opened
    await pipeline.stop()


async def test_ptt_only_opens_the_device_on_press_and_closes_it_on_release(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    pipeline = make_pipeline(audio_in, audio_out, listening_mode="ptt_only")
    await pipeline.start()

    await pipeline.push_to_talk(True)
    assert audio_in.enabled is True
    assert audio_in.opens == 1

    await pipeline.push_to_talk(False)
    assert audio_in.enabled is False
    await pipeline.stop()


async def test_continuous_mode_opens_the_device_once_at_start(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    pipeline = make_pipeline(audio_in, audio_out)
    await pipeline.start()
    assert audio_in.enabled is True
    assert audio_in.opens == 1
    await pipeline.stop()


async def test_muting_closes_the_device_and_unmuting_opens_it_again(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    pipeline = make_pipeline(audio_in, audio_out)
    await pipeline.start()

    await pipeline.set_muted(True)
    assert audio_in.enabled is False

    await pipeline.set_muted(False)
    assert audio_in.enabled is True
    assert audio_in.opens == 2
    await pipeline.stop()


async def test_a_dying_capture_loop_is_reported_as_unavailable(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    """A failing `frames()` used to leave the pipeline deaf while it still reported nothing."""
    pipeline = make_pipeline(audio_in, audio_out)
    await pipeline.start()
    assert pipeline.capture_health()[0] is HealthStatus.AVAILABLE

    audio_in.fail_with = RuntimeError("PortAudio device lost")
    audio_in.push(b"")  # any frame wakes the loop, which then raises
    await settle(20)

    status, reason = pipeline.capture_health()
    assert status is HealthStatus.UNAVAILABLE
    assert "PortAudio device lost" in reason
    await pipeline.stop()


async def test_a_failed_utterance_still_reports_tts_finished(
    audio_in: FakeAudioInput, audio_out: FakeAudioOutput
) -> None:
    """`say()` must never leave a caller waiting: every utterance ends in exactly one event."""
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    async def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("the output device disappeared mid-sentence")

    audio_out.play = boom  # type: ignore[method-assign]
    pipeline = DefaultVoicePipeline(
        audio_in=audio_in,
        audio_out=audio_out,
        stt=FakeStt(),
        tts=FakeTts(),
        emit=emit,
        capture_allowed=lambda: True,
        config=PipelineConfig(),
    )
    from nox.voice.base import TtsRequest

    try:
        await pipeline.say(TtsRequest(utterance_id="u1", text="Hallo"))
    except OSError:
        pass  # the caller still sees the real error

    finished = [payload for name, payload in events if name == E.TTS_FINISHED]
    assert finished == [{"utterance_id": "u1", "ok": False, "reason": "OSError"}]
