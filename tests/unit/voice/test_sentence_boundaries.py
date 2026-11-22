"""Where a sentence ends - the one rule the orchestrator and the engines share.

Every cut is a pause and a falling voice, so "am 9. Mai", "z. B.", "Dr. Meier" and "ca. 5" must
stay in one piece, while a real sentence end must still be one. Streamed text is held to the same
rule: the sentences that come out of a text fed in pieces are the ones that come out of the text
whole.
"""

from __future__ import annotations

import pytest

from nox.core.orchestrator import Orchestrator
from nox.voice.tts.sentences import split_sentences, split_stream


def whole(text: str) -> list[str]:
    return split_stream(text, final=True)[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # dates and ordinals
        (
            "Wir treffen uns am 9. Mai um fünf. Danach gehen wir.",
            ["Wir treffen uns am 9. Mai um fünf.", "Danach gehen wir."],
        ),
        ("Am 22.11. um fünf Uhr. Dann nicht.", ["Am 22.11. um fünf Uhr.", "Dann nicht."]),
        (
            "Es war der 3. Oktober 1989. Ein Freitag.",
            ["Es war der 3. Oktober 1989.", "Ein Freitag."],
        ),
        ("Das ist der 3. Versuch. Weiter.", ["Das ist der 3. Versuch.", "Weiter."]),
        (
            "Ich komme 9. Mai wieder. Dann gehe ich.",
            ["Ich komme 9. Mai wieder.", "Dann gehe ich."],
        ),
        # a number that does end a sentence
        ("Ich bin 25. Dann gehe ich.", ["Ich bin 25.", "Dann gehe ich."]),
        (
            "Es war im Jahr 2026. Danach kam das Ende.",
            ["Es war im Jahr 2026.", "Danach kam das Ende."],
        ),
        # abbreviations
        (
            "Das ist z. B. ein Test. Dr. Meier kommt ca. 5 Minuten später.",
            ["Das ist z. B. ein Test.", "Dr. Meier kommt ca. 5 Minuten später."],
        ),
        ("Also z.B. morgen. Prof. Lang auch.", ["Also z.B. morgen.", "Prof. Lang auch."]),
        (
            "Das gilt u. a. für Äpfel. Und für Birnen.",
            ["Das gilt u. a. für Äpfel.", "Und für Birnen."],
        ),
        # decimals and thousands
        ("Es kostet 3.5 Euro. Oder 1.500 Euro.", ["Es kostet 3.5 Euro.", "Oder 1.500 Euro."]),
        ("Es kostet 3,50 Euro. Teuer.", ["Es kostet 3,50 Euro.", "Teuer."]),
        # list markers
        (
            "Schritt 1. Öffne die Tür. Schritt 2. Geh hinaus.",
            ["Schritt 1. Öffne die Tür.", "Schritt 2. Geh hinaus."],
        ),
        ("1. Äpfel\n2. Birnen\n- Nüsse", ["1. Äpfel", "2. Birnen", "- Nüsse"]),
        # quotes and brackets stay with their sentence
        ('Er sagte: "Hallo." Dann ging er.', ['Er sagte: "Hallo."', "Dann ging er."]),
        ("Er fragte: »Wirklich?« Sie nickte.", ["Er fragte: »Wirklich?«", "Sie nickte."]),
        ("Das war gut (sehr gut.) Weiter.", ["Das war gut (sehr gut.)", "Weiter."]),
        # marks that always end a sentence
        ("Wirklich?! Ja... Na gut!", ["Wirklich?!", "Ja...", "Na gut!"]),
        ("Hallo! Ich bin Nox. Wie geht es dir?", ["Hallo!", "Ich bin Nox.", "Wie geht es dir?"]),
        # line breaks
        ("Erster Satz\n\nZweiter Satz  ", ["Erster Satz", "Zweiter Satz"]),
        # initials
        ("Ich traf J. Meier gestern. Er war nett.", ["Ich traf J. Meier gestern.", "Er war nett."]),
        ("", []),
        ("   \n ", []),
    ],
)
def test_sentences_end_where_a_sentence_ends(text: str, expected: list[str]) -> None:
    assert whole(text) == expected


def test_a_text_without_an_end_mark_is_one_sentence() -> None:
    assert whole("Ein Satz ohne Punkt") == ["Ein Satz ohne Punkt"]


# ---- streaming --------------------------------------------------------------------------------


def test_what_is_complete_comes_out_and_the_rest_stays() -> None:
    assert split_stream("Hallo. Wie geht", final=False) == (["Hallo."], "Wie geht")
    assert split_stream("Hallo. Wie geht", final=True) == (["Hallo.", "Wie geht"], "")
    assert split_stream("Hallo. ", final=False) == (["Hallo."], "")  # the white space decides it
    assert split_stream("Hallo.", final=False) == ([], "Hallo.")  # a mark at the end may grow


def test_a_number_with_a_full_stop_waits_for_the_next_word() -> None:
    """ "am 9." is a date or a sentence end: it is not said until the next word is known."""
    assert split_stream("Wir treffen uns am 9.", final=False) == ([], "Wir treffen uns am 9.")
    assert split_stream("Wir treffen uns am 9. ", final=False) == ([], "Wir treffen uns am 9. ")
    assert split_stream("Wir treffen uns am 9. Mai", final=False) == (
        [],
        "Wir treffen uns am 9. Mai",
    )
    assert split_stream("Ich bin 25. Dann", final=False) == (
        [],
        "Ich bin 25. Dann",
    )  # may be "Dann"
    assert split_stream("Ich bin 25. Dann ", final=False) == (["Ich bin 25."], "Dann ")


def test_when_the_stream_ends_the_held_back_text_is_said() -> None:
    assert split_stream("Wir treffen uns am 9.", final=True) == (["Wir treffen uns am 9."], "")


def test_a_decimal_that_is_still_arriving_is_not_cut() -> None:
    assert split_stream("Es kostet 3.", final=False) == ([], "Es kostet 3.")
    assert split_stream("Es kostet 3.5 Euro. ", final=False) == (["Es kostet 3.5 Euro."], "")


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 50])
@pytest.mark.parametrize(
    "text",
    [
        "Wir treffen uns am 9. Mai um fünf. Danach gehen wir. Dr. Meier kommt ca. 5 Min. später.",
        'Er sagte: "Hallo." Dann ging er. Es war der 3. Oktober 1989. Ich bin 25. Dann wirklich?!',
        "Schritt 1. Öffne die Tür. Es kostet 1.500 Euro, z. B. morgen. Ja... Na gut!\nNoch eine.",
    ],
)
def test_text_fed_in_pieces_gives_the_sentences_of_the_whole_text(text: str, size: int) -> None:
    sentences: list[str] = []
    buffer = ""
    for i in range(0, len(text), size):
        buffer += text[i : i + size]
        done, buffer = split_stream(buffer, final=False)
        sentences += done
    done, _ = split_stream(buffer, final=True)

    assert sentences + done == whole(text)


# ---- the engines and the orchestrator use the same rule ----------------------------------------


def test_the_engines_split_like_the_orchestrator_does() -> None:
    text = "Wir treffen uns am 9. Mai um fünf. Dr. Meier kommt ca. 5 Minuten später. Ja!"

    assert split_sentences(text, min_chars=0) == Orchestrator._split(text, final=True)[0]


def test_the_orchestrator_cuts_a_stream_where_a_sentence_ends() -> None:
    sentences, rest = Orchestrator._split("Am 9. Mai ist es so. Danach", final=False)

    assert sentences == ["Am 9. Mai ist es so."] and rest == "Danach"


def test_short_fragments_still_merge_for_the_engines() -> None:
    assert split_sentences("Ok. Dann machen wir das so. Ja.") == ["Ok. Dann machen wir das so. Ja."]
