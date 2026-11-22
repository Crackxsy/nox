"""Per-sentence style is an interface for later: carried to the engine unchanged, read by none."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nox.voice.base import SentenceStyle, TtsRequest
from tests.unit.voice.test_speech_pipelining import ClipEngine, make


def test_a_request_without_a_style_is_the_voice_as_it_is() -> None:
    request = TtsRequest(utterance_id="u1", text="Hallo.")

    assert request.style is None


def test_a_style_survives_the_trip_over_the_wire() -> None:
    request = TtsRequest(
        utterance_id="u1", text="Hallo.", style=SentenceStyle(emphasis=0.5, tempo=1.25)
    )

    again = TtsRequest.model_validate(request.model_dump(mode="json"))

    assert again.style == SentenceStyle(emphasis=0.5, tempo=1.25)


def test_a_request_from_an_older_core_without_the_field_is_still_valid() -> None:
    request = TtsRequest.model_validate({"utterance_id": "u1", "text": "Hallo."})

    assert request.style is None and request.rate == 1.0


@pytest.mark.parametrize(
    "values",
    [{"emphasis": 1.01}, {"emphasis": -1.01}, {"tempo": 0.0}, {"tempo": 0.24}, {"tempo": 4.1}],
)
def test_a_style_outside_its_range_is_refused(values: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        SentenceStyle(**values)


def test_a_style_has_no_fields_nobody_knows() -> None:
    with pytest.raises(ValidationError):
        SentenceStyle.model_validate({"emphasis": 0.1, "pitch": 3})


def test_the_defaults_change_nothing() -> None:
    assert SentenceStyle() == SentenceStyle(emphasis=0.0, tempo=1.0)


async def test_the_engine_gets_the_style_exactly_as_it_was_asked_for() -> None:
    pipeline, out, tts, _ = make()
    style = SentenceStyle(emphasis=-0.4, tempo=0.9)
    out.hold("s1")
    first = TtsRequest(utterance_id="s1", text="Eins.")
    second = TtsRequest(utterance_id="s2", text="Zwei.", style=style)  # prepared ahead of its turn

    import asyncio

    tasks = [asyncio.create_task(pipeline.say(r)) for r in (first, second)]
    await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)
    out.holds["s1"].set()
    await asyncio.gather(*tasks)

    assert [r.style for r in tts.requests] == [None, style]


async def test_the_style_stays_on_the_request_when_a_missing_clip_is_spoken_instead() -> None:
    engine = ClipEngine()
    engine.missing.add("careful")
    pipeline, _, _, _ = make(tts=engine)
    style = SentenceStyle(emphasis=1.0)

    await pipeline.say(
        TtsRequest(utterance_id="c1", text="Careful.", prepared_clip="careful", style=style)
    )

    assert [(r.prepared_clip, r.style) for r in engine.requests] == [
        ("careful", style),
        (None, style),
    ]
