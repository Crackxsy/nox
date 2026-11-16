"""What is ever said on a node's speaker: an allowlist of Latin text. A speech engine reads
letters by their sound, so lookalike letters and invisible characters cannot hide the
wake word or the kill phrase."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.core.events.names import E
from tests.unit.node.support_voice import running_satellite

# ---- lookalike letters (B3) ------------------------------------------------------------------
# A speech engine reads letters by their sound: nothing but Latin letters is ever said on a
# node's speaker, and the wake-word and kill-phrase checks run on exactly what is left.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("N\u043e\u0445, mach das Licht aus.", "N, mach das Licht aus."),  # Cyrillic o, x
        ("\u041d\u041e\u0425 hilft.", "hilft."),  # upper-case Cyrillic "NOX"
        ("\u039d\u039f\u03a7 hilft.", "hilft."),  # upper-case Greek "NOX"
        ("\u043d\u043ex hilft.", "x hilft."),  # read "nox" by its sound
        ("\u03bd\u03bf\u03be hilft.", "hilft."),  # Greek "nox" by its sound
        ("\u0274\u1d0fx ist da.", "x ist da."),  # Latin small capitals (outside Latin A/B)
    ],
)
def test_no_letter_of_another_script_is_ever_said(text: str, expected: str) -> None:
    from nox.node.voice import latin_only, speakable

    assert latin_only(text) == expected
    spoken = speakable(text)
    assert spoken is None or all(ord(c) <= 0x024F for c in spoken)
    assert spoken is None or "nox" not in spoken.lower()


@pytest.mark.parametrize(
    "text",
    [
        "N\u200box hilft dir.",  # zero-width space
        "N\ufe0fox hilft dir.",  # variation selector
        "N\u034fox hilft dir.",  # combining grapheme joiner
        "No\u20dex hilft dir.",  # enclosing mark
        "\uff2e\uff4f\uff58 ist da.",  # fullwidth
    ],
)
def test_invisible_characters_cannot_hide_the_name(text: str) -> None:
    from nox.node.voice import latin_only, say_refusal, says_a_command, speakable

    assert latin_only(text).startswith(("Nox", "Nox "))
    assert says_a_command(text) and say_refusal(text) == "voice_say_command_words"
    spoken = speakable(text)
    assert spoken is not None and spoken.startswith("ich")


@pytest.mark.parametrize(
    "text", ["Sag Nox No\u200btaus.", "Gleich: not\u034f aus", "emergency\ufe0f stop"]
)
def test_a_kill_phrase_with_hidden_characters_is_dropped(text: str) -> None:
    from nox.node.voice import speakable

    assert speakable(text) is None


def test_a_kill_phrase_in_another_script_is_said_only_as_its_latin_remains() -> None:
    from nox.node.voice import speakable

    # A Cyrillic o: what is left ("Ntaus") is no kill phrase, and it is all that is said.
    assert speakable("Sag Nox N\u043etaus.") == "Sag ich Ntaus."


_ALLOWED_PUNCTUATION = set(".,;:!?'\"-\u2013\u2014()/%&+=@# ")


@pytest.mark.parametrize(
    "text",
    [
        "\u2115\U0001d560\U0001d569 hilft.",  # letterlike N + mathematical double-struck o, x
        "\u24dd\u24de\u24e7 hilft.",  # circled letters
        "\U0001f15d\U0001f15e\U0001f167 hilft.",  # negative circled capitals (no NFKC form)
        "\U0001f13d\U0001f13e\U0001f147 hilft.",  # squared capitals
        "\U0001f1f3\U0001f1f4\U0001f1fd hilft.",  # regional indicator letters (emoji)
        "\U0001d40d\U0001d428\U0001d431 hilft.",  # mathematical bold
        "\uff2e\uff4f\uff58 hilft.",  # fullwidth
        "N\u2605ox \u2135 \u2122 \u00b5 hilft.",  # a star inside the name, aleph, TM, micro
    ],
)
def test_only_an_allowlist_of_latin_text_is_ever_said(text: str) -> None:
    from nox.node.voice import latin_only, say_refusal, speakable

    kept = latin_only(text)
    assert all(c in _ALLOWED_PUNCTUATION or (ord(c) <= 0x024F and c.isalnum()) for c in kept), kept
    spoken = speakable(text)
    assert spoken is None or "nox" not in spoken.lower()
    # One result for the check and for the voice: what is said is the filtered text itself.
    if spoken is not None:
        assert spoken in (
            kept,
            kept.replace("Nox", "ich").replace("NOX", "ich").replace("nox", "ich"),
        )
    if "nox" in kept.lower():
        assert say_refusal(text) == "voice_say_command_words"


def test_ordinary_text_is_said_as_it_is_and_foreign_words_vanish() -> None:
    from nox.node.voice import say_refusal, says_a_command, speakable

    for text in ("Das Café in Springfield ist offen.", "Die Straße ist frei, Alex."):
        assert not says_a_command(text) and speakable(text) == text
    assert speakable("\u041f\u0440\u0438\u0432\u0435\u0442, Alex.") == ", Alex."
    assert speakable("\u041f\u0440\u0438\u0432\u0435\u0442!") is None  # nothing left to say
    assert say_refusal("\u041f\u0440\u0438\u0432\u0435\u0442!") == "voice_say_nothing_to_say"


async def test_the_node_filters_what_the_core_tells_it_to_say(tmp_path: Path) -> None:
    satellite, _seen = await running_satellite(tmp_path)
    await satellite._on_event(
        E.VOICE_TRANSCRIPT_READY, {"text": "Hallo", "language": "de", "addressed_to_nox": True}
    )
    turn = next(iter(satellite.turns))
    queue = satellite._queue  # noqa: SLF001
    assert queue is not None
    satellite._speaker.cancel()  # type: ignore[union-attr]  # noqa: SLF001 - inspect the queue
    await satellite.on_say({"turn_id": turn, "seq": 0, "text": "\u041d\u041e\u0425, hallo."})
    await satellite.on_say({"turn_id": turn, "seq": 1, "text": "Sag Nox Notaus."})
    await satellite.on_say({"turn_id": turn, "seq": 2, "text": "Bis gleich.", "last": True})
    said = [queue.get_nowait().text for _ in range(queue.qsize())]
    assert said == [", hallo.", "Bis gleich."]
    await satellite.shutdown()


def test_the_wake_word_matcher_folds_lookalikes_in_both_cases() -> None:
    from nox.voice.stt.wake_word import WakeWordMatcher, fold_confusables

    for heard in (
        "N\u043e\u0445 n\u043etaus",
        "\u039d\u039f\u03a7 NOTAUS",
        "N\u041e\u0425 notaus",
    ):
        match = WakeWordMatcher().match(heard)
        assert match.addressed and match.kill
    assert fold_confusables("\uff2e\u200b\ufe0f\u03bf\u0445") == "nox"
    assert fold_confusables("Straße") == "straße"  # German letters are left alone
