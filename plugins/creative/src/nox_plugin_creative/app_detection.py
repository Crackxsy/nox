"""App-family pattern matching and mode-switch hysteresis (Spec v0.7 Creative Apps §3.1, ST-16-01).

Two independent, unit-testable pieces:

- `match_app_family` turns one `(process, title)` sample into a configured app-family id (or
  `None`), using `config.creative.app_patterns` (`nox.core.config.CreativeConfig`) - conservative
  by design (Spec v0.7 §11: a false negative is safer than a false positive).
- `HysteresisDetector` debounces a stream of family samples so a short alt-tab away from (and back
  to) a creative app never fires a spurious `creative.app_left`/`creative.app_detected` pair
  (Spec v0.7 §3.1 step 3/4, default window 15s - `config.creative.hysteresis_s`, pending approval).
  It is pure (an injectable clock, no asyncio) so the debounce logic itself is fully deterministic
  to test; the plugin drives it from real time via `nox_plugin_creative.plugin`.
"""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


def _glob(value: str, pattern: str) -> bool:
    return fnmatch.fnmatch(value.strip().lower(), pattern.strip().lower())


def match_app_family(
    process: str, title: str, patterns: Mapping[str, Sequence[Mapping[str, Any]]]
) -> str | None:
    """First app family whose pattern list has an entry matching `process`/`title`.

    An entry with both `process` and `title` set requires both to match (used for the
    browser-DAW family: a browser process AND the app's window-title pattern). An entry with
    neither field set never matches (a config typo should not become "match everything").
    """
    for family, entries in patterns.items():
        for entry in entries:
            proc_pattern = entry.get("process")
            title_pattern = entry.get("title")
            if not proc_pattern and not title_pattern:
                continue
            if proc_pattern and not _glob(process, str(proc_pattern)):
                continue
            if title_pattern and not _glob(title, str(title_pattern)):
                continue
            return family
    return None


class HysteresisDetector:
    """Debounces foreground-app-family samples over `window_s` seconds.

    Call `sample(family, now=...)` on every observation; call `resolve(now=...)` again from a
    scheduled timer (see `seconds_until_resolve`) so a sustained miss is still detected even when
    no further sample arrives (e.g. the user leaves the machine idle after leaving Blender).
    """

    def __init__(self, window_s: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self.window_s = window_s
        self._clock = clock
        self.current: str | None = None
        self._pending: str | None = None
        self._pending_since: float | None = None

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now

    def sample(self, family: str | None, *, now: float | None = None) -> str | None:
        """Feed one observation. Returns `"enter:<app>"` / `"leave:<app>"` if this sample itself
        crosses the hysteresis window (rare - usually `resolve()` via the scheduled timer does),
        else `None`.

        `_pending_since is None` is the sole "nothing in progress" flag, checked *before*
        comparing `family` to `self._pending`: `None` is both a legitimate family value (no
        creative app foregrounded) and `_pending`'s rest value, so comparing values first would
        conflate "nothing pending" with "pending transition to no-app" and silently never resolve
        a sustained `leave` (caught by `test_sustained_leave_fires_leave` - keep this ordering).
        """
        t = self._now(now)
        if family == self.current:
            # Back to the confirmed state: cancel any pending switch away from it (no-flap path).
            self._pending_since = None
            return None
        if self._pending_since is None or family != self._pending:
            self._pending = family
            self._pending_since = t
            return None
        return self.resolve(now=t)

    def resolve(self, *, now: float | None = None) -> str | None:
        """Apply the pending transition if `window_s` has elapsed since it started."""
        if self._pending_since is None:
            return None
        t = self._now(now)
        if t - self._pending_since < self.window_s:
            return None
        previous = self.current
        self.current = self._pending
        self._pending_since = None
        if self.current is not None:
            return f"enter:{self.current}"
        return f"leave:{previous}"

    def seconds_until_resolve(self, *, now: float | None = None) -> float | None:
        """How long until a scheduled `resolve()` call would apply the pending transition, or
        `None` if there is nothing pending."""
        if self._pending_since is None:
            return None
        t = self._now(now)
        return max(0.0, self.window_s - (t - self._pending_since))
