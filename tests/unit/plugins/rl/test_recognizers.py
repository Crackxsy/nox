"""Template-matching recognizers against synthetic HUD fixtures (Spec §12.1 - deterministic,
CI-safe; real-HUD-pixel accuracy is blocked on ES-04, see the module docstring in
`nox_plugin_rl.recognizers`)."""

from __future__ import annotations

from nox_plugin_rl import recognizers


def test_parse_boost_reads_0_to_100() -> None:
    templates = recognizers.render_digit_templates()
    for value in (0, 1, 50, 99, 100):
        crop = recognizers.render_digits(str(value))
        result = recognizers.parse_boost(crop, templates)
        assert result is not None
        assert result[0] == value
        assert result[1] > 0.5


def test_parse_score_reads_two_digit_values() -> None:
    templates = recognizers.render_digit_templates()
    crop = recognizers.render_digits("07")
    result = recognizers.parse_score(crop, templates)
    assert result is not None
    assert result[0] == 7


def test_parse_clock_reads_m_ss() -> None:
    templates = recognizers.render_digit_templates()
    crop = recognizers.render_clock("4", "12")
    result = recognizers.parse_clock(crop, templates)
    assert result is not None
    assert result[0] == "4:12"


def test_parse_clock_rejects_invalid_seconds_gracefully() -> None:
    """A blank/empty crop never produces a confident false reading (A129: silence over guessing)."""
    templates = recognizers.render_digit_templates()
    import numpy as np

    blank = np.zeros((24, 80), dtype="uint8")
    assert recognizers.parse_clock(blank, templates) is None
    assert recognizers.parse_boost(blank, templates) is None


def test_read_digits_returns_none_on_too_small_crop() -> None:
    templates = recognizers.render_digit_templates()
    import numpy as np

    tiny = np.zeros((5, 5), dtype="uint8")
    assert recognizers.read_digits(tiny, templates) is None
