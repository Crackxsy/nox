"""`match_app_family` + `HysteresisDetector` (Spec v0.7 §3.1, ST-16-01): pure unit tests, no
asyncio, no plugin wiring - see `test_manifest.py`/`test_tool_registry.py` for the wired plugin."""

from __future__ import annotations

from nox_plugin_creative.app_detection import HysteresisDetector, match_app_family

PATTERNS = {
    "blender": [{"process": "blender.exe"}],
    "fl_studio": [{"process": "fl64.exe"}, {"process": "fl.exe"}],
    "krita": [{"process": "krita.exe"}],
    "browser_daw": [
        {"process": "chrome.exe", "title": "*webdaw*"},
        {"process": "msedge.exe", "title": "*webdaw*"},
    ],
}


class TestMatchAppFamily:
    def test_matches_by_process_name(self) -> None:
        assert match_app_family("blender.exe", "Untitled.blend", PATTERNS) == "blender"

    def test_matches_case_insensitively(self) -> None:
        assert match_app_family("BLENDER.EXE", "", PATTERNS) == "blender"

    def test_no_match_returns_none(self) -> None:
        assert match_app_family("notepad.exe", "readme.txt", PATTERNS) is None

    def test_browser_requires_both_process_and_title(self) -> None:
        # Right process, wrong title: a random chrome tab is not the browser DAW.
        assert match_app_family("chrome.exe", "GitHub - my-repo", PATTERNS) is None
        # Right process, right title: matches.
        assert match_app_family("chrome.exe", "webdaw - Chrome", PATTERNS) == "browser_daw"
        # Right title, wrong process: not a browser at all.
        assert match_app_family("notepad.exe", "webdaw notes", PATTERNS) is None

    def test_conservative_false_negative_over_false_positive(self) -> None:
        # An unrelated process with a title that happens to contain "webdaw" text but is not
        # one of the configured browser processes must not match (Spec v0.7 §11 risk note).
        assert match_app_family("explorer.exe", "webdaw - File Explorer", PATTERNS) is None


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestHysteresisDetector:
    def test_sustained_match_fires_enter(self) -> None:
        clock = FakeClock()
        det = HysteresisDetector(15.0, clock=clock)
        assert det.sample("blender") is None
        clock.advance(15.0)
        assert det.resolve() == "enter:blender"
        assert det.current == "blender"

    def test_below_window_does_not_fire(self) -> None:
        clock = FakeClock()
        det = HysteresisDetector(15.0, clock=clock)
        det.sample("blender")
        clock.advance(14.9)
        assert det.resolve() is None
        assert det.current is None

    def test_short_alt_tab_does_not_flap(self) -> None:
        """Spec v0.7 acceptance criterion: alt-tabbing away for < window and back must not emit a
        spurious enter/leave pair."""
        clock = FakeClock()
        det = HysteresisDetector(15.0, clock=clock)
        det.sample("blender")
        clock.advance(15.0)
        assert det.resolve() == "enter:blender"

        # Alt-tab away for 5s, then back to blender - well within the window.
        det.sample(None)
        clock.advance(5.0)
        assert det.resolve() is None  # not yet resolved, but also...
        result = det.sample("blender")  # ...back to the confirmed state cancels the pending leave
        assert result is None
        clock.advance(15.0)
        assert det.resolve() is None  # nothing pending: no flap, no duplicate event
        assert det.current == "blender"

    def test_sustained_leave_fires_leave(self) -> None:
        clock = FakeClock()
        det = HysteresisDetector(15.0, clock=clock)
        det.sample("blender")
        clock.advance(15.0)
        det.resolve()
        det.sample(None)
        clock.advance(15.0)
        assert det.resolve() == "leave:blender"
        assert det.current is None

    def test_switching_between_two_families_fires_leave_then_enter(self) -> None:
        clock = FakeClock()
        det = HysteresisDetector(15.0, clock=clock)
        det.sample("blender")
        clock.advance(15.0)
        det.resolve()
        det.sample("krita")
        clock.advance(15.0)
        assert det.resolve() == "enter:krita"
        assert det.current == "krita"

    def test_seconds_until_resolve(self) -> None:
        clock = FakeClock()
        det = HysteresisDetector(15.0, clock=clock)
        assert det.seconds_until_resolve() is None
        det.sample("blender")
        clock.advance(4.0)
        remaining = det.seconds_until_resolve()
        assert remaining is not None
        assert abs(remaining - 11.0) < 1e-9
