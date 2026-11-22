"""A streamed answer reaches the speaker in sentences, cut where a sentence ends - not at every
full stop. Each cut is a pause, so "am 9. Mai" and "Dr. Meier" must stay in one piece."""

from __future__ import annotations

import pytest

from tests.unit.fakes import make_orchestrator as make


@pytest.mark.parametrize(
    ("answer", "spoken"),
    [
        (
            "Wir treffen uns am 9. Mai um fünf. Danach gehen wir.",
            ["Wir treffen uns am 9. Mai um fünf.", "Danach gehen wir."],
        ),
        (
            "Das ist z. B. ein Test. Dr. Meier kommt ca. 5 Minuten später.",
            ["Das ist z. B. ein Test.", "Dr. Meier kommt ca. 5 Minuten später."],
        ),
        ("Es kostet 3.5 Euro. Oder 1.500 Euro.", ["Es kostet 3.5 Euro.", "Oder 1.500 Euro."]),
        ("Ich bin 25. Dann gehe ich.", ["Ich bin 25.", "Dann gehe ich."]),
        ("Hallo. Ich bin Nox! Wie geht es dir?", ["Hallo.", "Ich bin Nox!", "Wie geht es dir?"]),
        ("Ein Satz ohne Punkt", ["Ein Satz ohne Punkt"]),
    ],
)
async def test_the_speaker_gets_sentences_not_fragments(answer: str, spoken: list[str]) -> None:
    orch, _, router, speaker, _ = make()
    router.text = answer  # streamed word by word
    await orch.start()

    await orch.handle_text("Erklär mir kurz Python")

    assert [r.text for r in speaker.said] == spoken
    await orch.stop()
