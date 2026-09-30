"""The only file allowed to synthesise input, tested only through the paths that refuse.

Not one test here sends a keystroke, and that is deliberate rather than cautious: a test that typed
for real would type into whatever window happened to have focus on the machine running the suite.
Every assertion is about something *not* happening.

The order of the checks is the property under test. `game_running` is answered before the platform,
before the text, before the foreground - because the answer "no" must not depend on getting any of
the rest right.
"""

from __future__ import annotations

import pytest

from nox.desktop.keyboard import MAX_CHARS, GameRunningError, TypingError, available, type_text


def test_a_running_game_stops_everything_first() -> None:
    """Checked before the platform and before the text, so no other bug can reach past it."""
    with pytest.raises(GameRunningError, match="a game is running"):
        type_text("hallo", game_running=True, expected_window=1)


def test_a_running_game_stops_even_an_empty_request() -> None:
    with pytest.raises(GameRunningError):
        type_text("", game_running=True, expected_window=0)


def test_a_running_game_stops_a_request_over_the_limit() -> None:
    with pytest.raises(GameRunningError):
        type_text("x" * (MAX_CHARS + 1), game_running=True, expected_window=0)


def test_nothing_to_type_is_refused() -> None:
    with pytest.raises(TypingError, match="nothing to type"):
        type_text("", game_running=False, expected_window=0)


def test_too_much_to_type_is_refused() -> None:
    """An unbounded loop of synthetic keystrokes is not something to leave to a language model."""
    with pytest.raises(TypingError, match=f"the limit is {MAX_CHARS}"):
        type_text("x" * (MAX_CHARS + 1), game_running=False, expected_window=0)


@pytest.mark.skipif(not available(), reason="the foreground check needs Windows")
def test_a_window_that_is_not_in_front_gets_nothing() -> None:
    """Handle 0 is never the foreground window, so this exercises the real check and sends nothing.

    This is the guard that matters most: focus moves for reasons this code does not control, and
    text typed into whatever is in front instead would make the whole feature indefensible.
    """
    with pytest.raises(TypingError, match="moved out of the foreground"):
        type_text("hallo", game_running=False, expected_window=0)


def test_the_error_for_a_game_is_the_one_a_caller_can_tell_apart() -> None:
    """`GameRunningError` is a `TypingError`, so a caller may catch either - and can distinguish."""
    assert issubclass(GameRunningError, TypingError)
