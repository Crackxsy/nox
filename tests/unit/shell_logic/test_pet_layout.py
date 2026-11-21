"""Where the pet window goes: the monitor it is meant for, and never off the screens that exist.

Pure geometry - no window, no display."""

from __future__ import annotations

import pytest

from nox.shell.pet_layout import (
    CORNER_MARGIN_PX,
    MIN_VISIBLE_FRACTION,
    Monitor,
    Rect,
    corrected_rect,
    initial_rect,
    pick_monitor,
)

LEFT = Monitor(Rect(0, 0, 1920, 1040), primary=True)
RIGHT = Monitor(Rect(1920, 0, 2560, 1400))
FAR_RIGHT = Monitor(Rect(4480, 0, 1280, 1000))
SIZE = (260, 300)


@pytest.mark.parametrize(
    ("setting", "monitors", "expected"),
    [
        ("left", [RIGHT, LEFT], LEFT),
        ("right", [LEFT, RIGHT], RIGHT),
        ("right", [LEFT, RIGHT, FAR_RIGHT], FAR_RIGHT),
        ("LEFT", [LEFT, RIGHT], LEFT),
        ("  right ", [LEFT, RIGHT], RIGHT),
        ("primary", [RIGHT, LEFT], LEFT),
        ("1", [RIGHT, LEFT], LEFT),
        ("2", [RIGHT, LEFT], RIGHT),
        ("3", [LEFT, RIGHT, FAR_RIGHT], FAR_RIGHT),
        ("3", [LEFT, RIGHT], LEFT),  # no third monitor today: the primary one
        ("0", [LEFT, RIGHT], LEFT),
        ("-1", [LEFT, RIGHT], LEFT),
        ("", [LEFT, RIGHT], LEFT),
        ("middle", [LEFT, RIGHT], LEFT),
    ],
)
def test_the_setting_picks_a_monitor(
    setting: str, monitors: list[Monitor], expected: Monitor
) -> None:
    assert pick_monitor(setting, monitors) == expected


def test_the_primary_monitor_is_the_fallback_even_when_it_is_not_on_the_left() -> None:
    primary_right = Monitor(Rect(1920, 0, 2560, 1400), primary=True)
    monitors = [Monitor(Rect(0, 0, 1920, 1040)), primary_right]
    assert pick_monitor("nonsense", monitors) == primary_right


def test_without_a_primary_flag_the_leftmost_monitor_is_used() -> None:
    leftmost = Monitor(Rect(0, 0, 800, 600))
    assert pick_monitor("primary", [RIGHT, leftmost]) == leftmost


def test_no_monitors_means_no_answer() -> None:
    assert pick_monitor("left", []) is None
    assert initial_rect(None, SIZE, "left", []) is None


def test_a_new_pet_opens_in_the_bottom_right_corner_of_its_monitor() -> None:
    rect = initial_rect(None, SIZE, "right", [LEFT, RIGHT])
    assert rect == Rect(
        RIGHT.area.right - SIZE[0] - CORNER_MARGIN_PX,
        RIGHT.area.bottom - SIZE[1] - CORNER_MARGIN_PX,
        *SIZE,
    )


def test_a_saved_position_that_is_still_on_a_screen_is_kept() -> None:
    assert initial_rect((100, 200), SIZE, "right", [LEFT, RIGHT]) == Rect(100, 200, *SIZE)


def test_a_saved_position_on_a_monitor_that_is_gone_goes_to_the_default_monitor() -> None:
    saved_on_the_unplugged_one = (FAR_RIGHT.area.x + 100, 100)
    rect = initial_rect(saved_on_the_unplugged_one, SIZE, "left", [LEFT, RIGHT])
    assert rect is not None
    assert (rect.x, rect.y) == (
        LEFT.area.right - SIZE[0] - CORNER_MARGIN_PX,
        LEFT.area.bottom - SIZE[1] - CORNER_MARGIN_PX,
    )


@pytest.mark.parametrize(
    "window",
    [
        Rect(0, 0, 260, 300),
        Rect(1920 - 260, 740, 260, 300),
        Rect(-130, 100, 260, 300),  # exactly half off the left edge is still allowed
        Rect(1800, 100, 260, 300),  # straddling two monitors
    ],
)
def test_a_mostly_visible_window_is_left_alone(window: Rect) -> None:
    assert corrected_rect(window, "left", [LEFT, RIGHT]) == window


def test_the_visible_share_is_the_documented_one() -> None:
    assert MIN_VISIBLE_FRACTION == 0.5


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (Rect(-200, 100, 260, 300), Rect(0, 100, 260, 300)),  # 60 of 260 px visible
        (Rect(100, -250, 260, 300), Rect(100, 0, 260, 300)),
        (Rect(100, 1000, 260, 300), Rect(100, 740, 260, 300)),
    ],
)
def test_a_mostly_hidden_window_is_pulled_back_inside_its_nearest_monitor(
    window: Rect, expected: Rect
) -> None:
    assert corrected_rect(window, "right", [LEFT]) == expected


def test_a_window_on_no_monitor_goes_to_the_default_monitor_not_the_nearest() -> None:
    nowhere = Rect(9000, 9000, *SIZE)
    fixed = corrected_rect(nowhere, "left", [LEFT, RIGHT])
    assert LEFT.area.overlap_area(fixed) == fixed.area


def test_a_window_beyond_the_edge_of_a_monitor_counts_as_off_screen() -> None:
    beyond = Rect(LEFT.area.right + 5000, 0, *SIZE)
    assert corrected_rect(beyond, "left", [LEFT]) != beyond


def test_a_window_larger_than_the_monitor_keeps_its_top_left_corner_visible() -> None:
    tiny = Monitor(Rect(0, 0, 200, 200), primary=True)
    fixed = corrected_rect(Rect(-300, -300, 260, 300), "left", [tiny])
    assert (fixed.x, fixed.y) == (0, 0)


def test_the_corner_of_a_tiny_monitor_stays_inside_it() -> None:
    tiny = Monitor(Rect(0, 0, 200, 200), primary=True)
    rect = initial_rect(None, SIZE, "left", [tiny])
    assert rect is not None
    assert (rect.x, rect.y) == (0, 0)


def test_with_no_monitor_information_the_window_stays_where_it_is() -> None:
    window = Rect(-5000, 40, *SIZE)
    assert corrected_rect(window, "left", []) == window


def test_overlap_and_distance_helpers() -> None:
    a = Rect(0, 0, 10, 10)
    assert a.overlap_area(Rect(5, 5, 10, 10)) == 25
    assert a.overlap_area(Rect(10, 0, 5, 5)) == 0  # sharing an edge is not overlapping
    assert a.overlap_area(Rect(20, 20, 5, 5)) == 0
    assert a.center_distance_sq(Rect(10, 0, 10, 10)) == 20 * 20


def test_a_monitor_that_is_going_is_taken_out_by_its_area() -> None:
    from nox.shell.pet_layout import without_monitor

    assert without_monitor([LEFT, RIGHT], RIGHT.area) == [LEFT]
    assert without_monitor([LEFT, RIGHT], Rect(1, 2, 3, 4)) == [LEFT, RIGHT]


def test_a_window_on_a_monitor_that_is_still_listed_but_going_is_brought_back() -> None:
    """Qt may still list the screen it announces as removed: without taking it out, the window
    would count as visible and stay on a screen that is gone."""
    from nox.shell.pet_layout import without_monitor

    window = Rect(RIGHT.area.x + 100, 100, *SIZE)
    assert corrected_rect(window, "left", [LEFT, RIGHT]) == window  # the stale list keeps it
    fixed = corrected_rect(window, "left", without_monitor([LEFT, RIGHT], RIGHT.area))
    assert LEFT.area.overlap_area(fixed) == fixed.area
