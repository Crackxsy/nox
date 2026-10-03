"""Spoken mode switches: "lass uns streamen" starts the stream, "Stream beenden" ends it.

Saying "lass uns streamen" used to reach the language model, which talked about streaming and
switched nothing - the product owner's first session. A mode switch is a command, not a topic, so
it is recognised here, in front of the model, the same way presets are.

The rule this module must not break is the fast path's: **it may not swallow a real question or a
statement about streaming.** So a sentence is only a command when it is short, carries no question
mark, does not start with a question word, is not negated, and pairs an invitation ("lass uns",
"ich will", "starte") with the stream itself. "Wann wollen wir streamen?", "Ich will heute nicht
streamen" and "Gestern haben wir gestreamt" all go to the model.

The answer is honest about what happened: it names each stream plugin that is running, and the
reason for each one that is not, rather than announcing a stream that cannot reach Twitch.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from nox.core.logging import get_logger
from nox.core.state import Mode

log = get_logger(__name__)

__all__ = ["ModeGate", "PluginStatus", "match_mode_intent"]

#: Longest sentence still treated as a command. "Lass uns jetzt Minecraft auf Twitch streamen" is 7.
MAX_WORDS = 10

_QUESTION_WORDS = frozenset(
    {
        "wann", "warum", "wieso", "weshalb", "wie", "was", "wo", "wer", "welche", "welcher",
        "sollen", "sollten", "können", "kannst", "willst", "magst",
        "when", "why", "how", "what", "should", "can", "do", "does",
    }
)  # fmt: skip
_NEGATION = re.compile(r"\b(nicht|kein|keinen|keine|nie|niemals|not|never|dont)\b")

_START = [
    re.compile(r"\b(lass|lasst)( uns)?\b.*\b(streamen|stream|live)\b"),
    re.compile(
        r"\b(ich will|ich möchte|wir wollen|wir gehen|ich gehe|wir sind)\b.*\b(streamen|live)\b"
    ),
    re.compile(r"\b(starte|start|beginne|mach)\b.*\bstream\b"),
    re.compile(r"\b(stream|streaming) ?(modus|mode)\b"),
    re.compile(r"\b(lets|let us) (stream|go live)\b"),
]
_END = [
    re.compile(r"\bstream\b.*\b(beenden|beende|aus|stoppen|stopp|ende|vorbei|schluss)\b"),
    re.compile(r"\b(beende|stoppe|stopp|end|stop)\b.*\bstream\b"),
    re.compile(r"\b(begleiter|normal|normaler|normalen) ?(modus|mode)\b"),
]

_APOSTROPHE = re.compile(r"['`´’]")
_PUNCTUATION = re.compile(r"[!.…,;:\"-]+")
_WHITESPACE = re.compile(r"\s+")
_ADDRESS = re.compile(r"^nox\s+|\s+nox$")


def _normalise(text: str) -> str:
    folded = _APOSTROPHE.sub("", text.strip().lower())
    folded = _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", folded)).strip()
    return _ADDRESS.sub("", folded).strip()


def match_mode_intent(text: str) -> Mode | None:
    """`Mode.STREAM` or `Mode.COMPANION` when `text` is a spoken mode switch, otherwise None."""
    if "?" in text:
        return None
    normalised = _normalise(text)
    words = normalised.split()
    if not words or len(words) > MAX_WORDS or words[0] in _QUESTION_WORDS:
        return None
    if _NEGATION.search(normalised):
        return None
    if any(pattern.search(normalised) for pattern in _END):
        return Mode.COMPANION
    if any(pattern.search(normalised) for pattern in _START):
        return Mode.STREAM
    return None


@dataclass(frozen=True, slots=True)
class PluginStatus:
    """One plugin's state after a switch: running, or not running with the reason (empty while
    it is still starting)."""

    plugin_id: str
    running: bool
    reason: str = ""


#: Spoken names; an id this table does not know is said as it is.
_PLUGIN_NAMES = {"twitch": "Twitch", "obs": "OBS", "clips": "Clips"}

SwitchMode = Callable[[Mode], Awaitable[str]]
StreamPlugins = Callable[[], Sequence[PluginStatus]]


def _name(plugin_id: str) -> str:
    return _PLUGIN_NAMES.get(plugin_id, plugin_id)


@dataclass(slots=True)
class ModeGate:
    """The orchestrator's mode gate: a spoken switch runs, and the reply says how it went.

    `switch` changes the mode and its profile (`nox.core.modes.switch_mode`). `stream_plugins`
    reports the plugins the stream profile runs; their workers need a moment after the switch, so
    the gate waits up to `settle_s` for each to be running or to have failed before it answers.
    """

    switch: SwitchMode
    current_mode: Callable[[], str]
    stream_plugins: StreamPlugins
    settle_s: float = 8.0
    poll_s: float = 0.25
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)

    async def handle(self, text: str, language: str) -> str | None:
        target = match_mode_intent(text)
        if target is None:
            return None
        german = not language.lower().startswith("en")
        if target is Mode.COMPANION:
            if self.current_mode() != Mode.STREAM.value:
                return None  # "Begleitermodus" while not streaming is talk, not a command
            await self.switch(Mode.COMPANION)
            log.info("mode_intent.stream_ended")
            if german:
                return "Stream-Modus aus. Ich bin wieder ganz bei dir."
            return "Stream mode off. I'm all yours again."
        if self.current_mode() == Mode.STREAM.value:
            return self._report(already=True, german=german)
        profile = await self.switch(Mode.STREAM)
        log.info("mode_intent.stream_started", profile=profile)
        await self._wait_for_plugins()
        return self._report(already=False, german=german)

    async def _wait_for_plugins(self) -> None:
        waited = 0.0
        while waited < self.settle_s:
            if all(s.running or s.reason for s in self.stream_plugins()):
                return
            await self.sleep(self.poll_s)
            waited += self.poll_s

    def _report(self, *, already: bool, german: bool) -> str:
        statuses = list(self.stream_plugins())
        running = [_name(s.plugin_id) for s in statuses if s.running]
        broken = [s for s in statuses if not s.running]
        if german:
            parts = ["Wir sind schon im Stream-Modus." if already else "Stream-Modus ist an."]
            if not statuses:
                parts.append(
                    "Aber kein Stream-Plugin ist eingeschaltet, schau in die Einstellungen."
                )
            if running:
                verb = "läuft" if len(running) == 1 else "laufen"
                parts.append(f"{' und '.join(running)} {verb}.")
            parts.extend(
                f"{_name(s.plugin_id)} läuft nicht: {s.reason or 'startet noch'}." for s in broken
            )
            return " ".join(parts)
        parts = ["We're already in stream mode." if already else "Stream mode is on."]
        if not statuses:
            parts.append("But no stream plugin is enabled, check the settings.")
        if running:
            verb = "is" if len(running) == 1 else "are"
            parts.append(f"{' and '.join(running)} {verb} running.")
        parts.extend(
            f"{_name(s.plugin_id)} is not running: {s.reason or 'still starting'}." for s in broken
        )
        return " ".join(parts)
