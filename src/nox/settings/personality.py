"""`personality.md`: the character text, as a file the user owns.

Nox's personality is a product of an interview with its owner, not a property of this source tree
- the repository ships a neutral default (`nox.ai.prompting.DEFAULT_PERSONALITY_BLOCK`) and every
installation keeps its own text in `<paths.data_dir>/personality.md`, created from that default on
first start. Editing it is a supported action (`personality.set` from the dashboard, or just
opening the file in an editor), so the loader re-reads on every mtime change instead of caching
the text for the process lifetime.

`RULES_BLOCK` deliberately stays in code: the data-block rule, the "you cannot execute anything"
rule and the "never reveal secrets" rule are security controls, and a security control that a text
field can delete is not one.
"""

from __future__ import annotations

from pathlib import Path

from nox.ai.prompting import DEFAULT_PERSONALITY_BLOCK
from nox.core.logging import get_logger

log = get_logger(__name__)

PERSONALITY_FILENAME = "personality.md"

#: Placeholders the default text uses; filled in from `identity` so a fresh install reads sensibly
#: before anyone has written a line of their own.
USER_NAME_PLACEHOLDER = "{user_name}"
ASSISTANT_NAME_PLACEHOLDER = "{assistant_name}"


def render_default(*, assistant_name: str = "Nox", user_name: str = "") -> str:
    """The neutral built-in text with its placeholders filled in."""
    return (
        str(DEFAULT_PERSONALITY_BLOCK)
        .replace(ASSISTANT_NAME_PLACEHOLDER, assistant_name or "Nox")
        .replace(USER_NAME_PLACEHOLDER, user_name or "the user")
    )


class PersonalityFile:
    """One installation's `personality.md`: create on first read, mtime-checked cache, write."""

    def __init__(self, path: Path, *, assistant_name: str = "Nox", user_name: str = "") -> None:
        self.path = path
        self._assistant_name = assistant_name
        self._user_name = user_name
        self._cached: str | None = None
        self._cached_stamp: tuple[int, int] | None = None

    def default_text(self) -> str:
        return render_default(assistant_name=self._assistant_name, user_name=self._user_name)

    def ensure(self) -> Path:
        """Create the file from the neutral default if it does not exist yet. Never overwrites."""
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.default_text(), encoding="utf-8")
            log.info("personality.created", path=str(self.path))
        return self.path

    def read(self) -> str:
        """The current text. A user edit wins over the built-in default, immediately.

        An unreadable file is reported and falls back to the default rather than taking the core
        down or, worse, silently running with an empty personality.
        """
        try:
            self.ensure()
            stat = self.path.stat()
        except OSError as exc:
            log.warning("personality.unreadable", path=str(self.path), error=str(exc))
            return self.default_text()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cached is not None and stamp == self._cached_stamp:
            return self._cached
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("personality.unreadable", path=str(self.path), error=str(exc))
            return self.default_text()
        self._cached = text
        self._cached_stamp = stamp
        return text

    def write(self, text: str) -> None:
        """Replace the text (dashboard `personality.set`). Empty input restores the default."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text if text.strip() else self.default_text(), encoding="utf-8")
        self._cached = None
        self._cached_stamp = None
        log.info("personality.written", path=str(self.path), chars=len(text))
