"""Where the pet window goes: which monitor, and never off the screens that exist.

Pure geometry, no Qt: the window and the screens come in as plain rectangles so every rule is
testable without a display. `nox.shell.pet_window` turns Qt's screens into `Monitor`s and applies
the answer.

Three rules, in this order:

* A window that is mostly visible stays where it is. Dragging a pet half over the screen edge is a
  choice, not a fault.
* A window that is mostly off-screen is moved back inside the monitor it is nearest to.
* A window that is on no monitor at all (its monitor was unplugged, the resolution shrank, the
  saved position came from another setup) goes to the corner of the configured default monitor.

`pet.default_monitor` is `left`, `right`, `primary`, or a number counted from the left (`1` is the
leftmost monitor). A value that matches no monitor falls back to the primary one, so a setting for a
monitor that is not plugged in today never leaves the pet without a place.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "CORNER_MARGIN_PX",
    "MIN_VISIBLE_FRACTION",
    "Monitor",
    "Rect",
    "corrected_rect",
    "initial_rect",
    "pick_monitor",
    "without_monitor",
]

#: How much of the window has to be on a monitor before it counts as visible.
MIN_VISIBLE_FRACTION = 0.5
#: Gap between the window and the monitor's edge when Nox picks the position.
CORNER_MARGIN_PX = 24


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def overlap_area(self, other: Rect) -> int:
        """Area shared with `other` (0 when they only touch or do not meet)."""
        width = min(self.right, other.right) - max(self.x, other.x)
        height = min(self.bottom, other.bottom) - max(self.y, other.y)
        return width * height if width > 0 and height > 0 else 0

    def center_distance_sq(self, other: Rect) -> int:
        """Squared distance between the two centres (twice the coordinates, to stay integral)."""
        dx = (self.x * 2 + self.width) - (other.x * 2 + other.width)
        dy = (self.y * 2 + self.height) - (other.y * 2 + other.height)
        return dx * dx + dy * dy


@dataclass(frozen=True, slots=True)
class Monitor:
    """One display: the area a window may use (the task bar is not part of it) and whether it is
    the one Windows calls primary."""

    area: Rect
    primary: bool = False


def without_monitor(monitors: Sequence[Monitor], area: Rect) -> list[Monitor]:
    """`monitors` minus the one whose usable area is `area`. Qt announces a screen's removal while
    the screen can still be in its own list, so the one that is going is taken out by hand."""
    return [monitor for monitor in monitors if monitor.area != area]


def _left_to_right(monitors: Sequence[Monitor]) -> list[Monitor]:
    return sorted(monitors, key=lambda m: (m.area.x, m.area.y))


def pick_monitor(setting: str, monitors: Sequence[Monitor]) -> Monitor | None:
    """The monitor `pet.default_monitor` names; the primary one when it names none that exists;
    None only when there are no monitors at all."""
    if not monitors:
        return None
    ordered = _left_to_right(monitors)
    primary = next((m for m in ordered if m.primary), ordered[0])
    wanted = setting.strip().lower()
    if wanted == "left":
        return ordered[0]
    if wanted == "right":
        return ordered[-1]
    if wanted.isdecimal() and 1 <= int(wanted) <= len(ordered):
        return ordered[int(wanted) - 1]
    return primary


def _corner(width: int, height: int, monitor: Monitor) -> Rect:
    """Bottom-right of the monitor's usable area, inside it even when the window is larger."""
    area = monitor.area
    x = max(area.x, area.right - width - CORNER_MARGIN_PX)
    y = max(area.y, area.bottom - height - CORNER_MARGIN_PX)
    return Rect(x, y, width, height)


def _inside(window: Rect, monitor: Monitor) -> Rect:
    """`window` moved the shortest way until it lies fully inside the monitor's usable area (its
    top-left corner wins when the window is larger than the monitor)."""
    area = monitor.area
    x = max(area.x, min(window.x, area.right - window.width))
    y = max(area.y, min(window.y, area.bottom - window.height))
    return Rect(x, y, window.width, window.height)


def _nearest(window: Rect, monitors: Sequence[Monitor]) -> Monitor:
    return max(
        monitors,
        key=lambda m: (window.overlap_area(m.area), -window.center_distance_sq(m.area)),
    )


def corrected_rect(window: Rect, setting: str, monitors: Sequence[Monitor]) -> Rect:
    """`window` as it should be now: unchanged when mostly visible, pulled back inside its nearest
    monitor when mostly off-screen, on the default monitor when on none. With no monitor
    information at all it is returned unchanged."""
    if not monitors or window.area == 0:
        return window
    visible = sum(window.overlap_area(m.area) for m in monitors)
    if visible == 0:
        default = pick_monitor(setting, monitors)
        assert default is not None  # monitors is not empty
        return _corner(window.width, window.height, default)
    if visible >= window.area * MIN_VISIBLE_FRACTION:
        return window
    return _inside(window, _nearest(window, monitors))


def initial_rect(
    saved: tuple[int, int] | None,
    size: tuple[int, int],
    setting: str,
    monitors: Sequence[Monitor],
) -> Rect | None:
    """Where the pet window opens: its saved position when that is still on a screen, otherwise
    the corner of the default monitor. None when there is neither a saved position nor a monitor,
    so the toolkit may choose."""
    width, height = size
    if saved is not None:
        return corrected_rect(Rect(saved[0], saved[1], width, height), setting, monitors)
    default = pick_monitor(setting, monitors)
    return None if default is None else _corner(width, height, default)
