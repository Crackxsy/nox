"""The engine is given the words as they are said; everyone else sees the text as it was written."""

from __future__ import annotations

from nox.core.events import E
from nox.voice.base import TtsRequest
from tests.unit.voice.test_speech_pipelining import ClipEngine, make


async def test_the_engine_gets_numbers_dates_and_abbreviations_in_words() -> None:
    pipeline, _, tts, _ = make()

    await pipeline.say(
        TtsRequest(
            utterance_id="s1", text="Am 9. Mai um 14:30 Uhr kostet es 1.500 Euro, z. B. heute."
        )
    )

    assert [r.text for r in tts.requests] == [
        "Am neunten Mai um vierzehn Uhr dreißig kostet es eintausendfünfhundert Euro, "
        "zum Beispiel heute."
    ]


async def test_the_text_in_the_events_is_the_text_that_was_asked_for() -> None:
    pipeline, _, _, events = make()
    text = "Es sind 4 °C."

    await pipeline.say(TtsRequest(utterance_id="s1", text=text))

    assert events.of(E.TTS_STARTED)[0]["text"] == text


async def test_a_request_can_ask_for_its_text_to_be_read_exactly() -> None:
    pipeline, _, tts, _ = make()

    await pipeline.say(TtsRequest(utterance_id="s1", text="Es sind 4 °C.", normalize=False))

    assert [r.text for r in tts.requests] == ["Es sind 4 °C."]


async def test_the_whole_step_can_be_switched_off() -> None:
    pipeline, _, tts, _ = make(normalize_text=False)

    await pipeline.say(TtsRequest(utterance_id="s1", text="Es sind 4 °C."))

    assert [r.text for r in tts.requests] == ["Es sind 4 °C."]


async def test_english_text_gets_the_english_rules() -> None:
    pipeline, _, tts, _ = make()

    await pipeline.say(TtsRequest(utterance_id="s1", text="Pick one, e.g. 5%.", language="en"))

    assert [r.text for r in tts.requests] == ["Pick one, for example 5 percent."]


async def test_the_other_fields_of_the_request_reach_the_engine_unchanged() -> None:
    pipeline, _, tts, _ = make()
    request = TtsRequest(utterance_id="s1", text="Es sind 4 °C.", voice="F3", rate=1.2)

    await pipeline.say(request)

    got = tts.requests[0]
    assert (got.utterance_id, got.voice, got.rate, got.channel) == (
        "s1",
        "F3",
        1.2,
        request.channel,
    )


async def test_a_clip_that_is_missing_is_spoken_from_the_words_in_words() -> None:
    engine = ClipEngine()
    engine.missing.add("careful")
    pipeline, out, _, _ = make(tts=engine)

    await pipeline.say(
        TtsRequest(utterance_id="c1", text="Noch 5 Sekunden.", prepared_clip="careful")
    )

    assert out.heard("c1") == [b"spoken:Noch f\xc3\xbcnf Sekunden."]
