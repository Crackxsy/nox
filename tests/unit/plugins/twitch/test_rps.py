"""`!rps` resolution logic and per-viewer cooldown (`nox_plugin_twitch.rps`)."""

from __future__ import annotations

from nox_plugin_twitch.rps import ALIASES, GERMAN_ALIASES, RockPaperScissors


def test_aliases_map_german_and_english_to_canonical_choices() -> None:
    assert ALIASES["stein"] == "rock"
    assert ALIASES["schere"] == "scissors"
    assert ALIASES["papier"] == "paper"
    assert ALIASES["rock"] == "rock"
    assert ALIASES["scissors"] == "scissors"
    assert ALIASES["paper"] == "paper"
    assert GERMAN_ALIASES == {"stein", "schere", "papier"}


def test_rock_beats_scissors() -> None:
    game = RockPaperScissors(choice_provider=lambda: "scissors")
    result = game.play("v1", "rock")
    assert result.outcome == "win"
    assert result.bot_choice == "scissors"


def test_scissors_beats_paper() -> None:
    game = RockPaperScissors(choice_provider=lambda: "paper")
    result = game.play("v1", "scissors")
    assert result.outcome == "win"


def test_paper_beats_rock() -> None:
    game = RockPaperScissors(choice_provider=lambda: "rock")
    result = game.play("v1", "paper")
    assert result.outcome == "win"


def test_bot_wins_when_it_beats_the_viewer() -> None:
    game = RockPaperScissors(choice_provider=lambda: "paper")
    result = game.play("v1", "rock")
    assert result.outcome == "lose"


def test_same_choice_is_a_tie() -> None:
    game = RockPaperScissors(choice_provider=lambda: "rock")
    result = game.play("v1", "rock")
    assert result.outcome == "tie"


def test_per_viewer_cooldown_blocks_a_second_round() -> None:
    now = [0.0]
    game = RockPaperScissors(cooldown_s=30.0, choice_provider=lambda: "rock", clock=lambda: now[0])
    assert game.cooldown_remaining("v1") == 0.0
    game.play("v1", "paper")
    now[0] = 5.0
    remaining = game.cooldown_remaining("v1")
    assert remaining == 25.0


def test_cooldown_is_per_viewer() -> None:
    now = [0.0]
    game = RockPaperScissors(cooldown_s=30.0, choice_provider=lambda: "rock", clock=lambda: now[0])
    game.play("v1", "paper")
    assert game.cooldown_remaining("v2") == 0.0


def test_cooldown_expires() -> None:
    now = [0.0]
    game = RockPaperScissors(cooldown_s=10.0, choice_provider=lambda: "rock", clock=lambda: now[0])
    game.play("v1", "paper")
    now[0] = 11.0
    assert game.cooldown_remaining("v1") == 0.0
