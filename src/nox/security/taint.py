"""Taint: which conversations hold content Nox did not author, and which tool results put it there.

The Security Model's "tainted turn" rule, made precise:

* **Every tool result taints, unless the tool is trusted-internal.** A shell command's stdout, a
  vault note, a memory hit, a calendar entry, a transcript, a window title, a screenshot, an MCP or
  browser result - any of it can carry text written by someone else, including instructions aimed
  at the model (prompt injection). Only tools whose results Nox composes entirely itself from its
  own state are exempt (`TRUSTED_INTERNAL_TOOLS`). An unknown tool taints.
* **Taint belongs to the conversation, not the turn.** The persistent Claude session keeps every
  earlier tool result in its context, and the orchestrator replays the conversation's recent turns
  into every model call, so content read three turns ago is still "in the conversation". Taint is
  therefore cleared only when that conversation's context is really discarded - the orchestrator
  starts a fresh conversation (the user asked for a new conversation, or it sat idle longer than
  `OrchestratorConfig.conversation_idle_s`) - never at the start of the next turn. Nox has no
  context compaction: the replayed history is a sliding window, but the warm Claude session keeps
  what slid out of it, so the window moving on clears nothing.
* **One conversation per channel and client.** Voice, each dashboard client and each paired phone
  have a conversation of their own (`Orchestrator.handle_text(channel=...)`), with their own
  history, their own model context (another conversation replaces the warm session) and their own
  taint. Each call carries its conversation key (`CONVERSATION`, set by the tool loop around the
  call), the executor copies it into the `PermissionRequest`, and the engine keeps a `TaintLedger`
  entry per key, so a page read on the phone never makes the desktop ask.
* **User-directed lookups stay quiet.** A tainted conversation still lets a read-only lookup run
  without a confirmation when its free text demonstrably comes from the user and not from what a
  tool returned (`TaintLedger.user_directed`): a `web.search` whose query copies no run of
  `COPIED_RUN_CHARS` characters and no host from untrusted content (unless the user typed it this
  turn), a `web.fetch` of a URL the user typed this turn, a curated read-only shell command none of
  whose arguments appears in untrusted content. Anything else - a link from a search result, a
  query that quotes a page, a result Nox could not read as text - confirms as before.

What a taint changes is decided in `nox.security.permissions`: a remembered grant above LOW asks
again, a curated read-only shell command asks, and any call that sends data off the machine
(network tools, declared egress) asks - so injected text cannot make Nox exfiltrate silently.
"""

from __future__ import annotations

import re
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import unquote

__all__ = [
    "CONVERSATION",
    "COPIED_RUN_CHARS",
    "MAX_UNTRUSTED_CHARS",
    "TRUSTED_INTERNAL_TOOLS",
    "LookupKind",
    "TaintLedger",
    "current_conversation",
    "result_taints",
    "untrusted_text",
]

#: The conversation the current tool call belongs to; empty outside a conversation.
CONVERSATION: ContextVar[str] = ContextVar("nox_conversation", default="")

#: Tools whose result Nox composes entirely from its own state: no third-party text can be in it.
#: Keep this list short and justify every entry; when in doubt a tool is not trusted.
TRUSTED_INTERNAL_TOOLS: frozenset[str] = frozenset(
    {
        "time.now",  # the clock
        "capabilities.list",  # Nox's own capability report
        "capabilities.check",  # ... and its rule lookup
        "file.roots",  # the configured root folders (paths the user set, not file content)
    }
)


def current_conversation() -> str:
    return CONVERSATION.get()


def result_taints(tool: str) -> bool:
    """Whether a result of `tool` may carry content Nox did not author (default: yes)."""
    return tool not in TRUSTED_INTERNAL_TOOLS


#: A run of this many characters copied from untrusted content makes a lookup not user-directed.
COPIED_RUN_CHARS = 20
#: Untrusted text kept per conversation for the provenance check. A conversation that read more is
#: "opaque": nothing in it counts as user-directed any more (it confirms, as before).
MAX_UNTRUSTED_CHARS = 512_000
#: Free text longer than this is never checked, only confirmed.
MAX_CHECKED_CHARS = 2_048
#: A shell argument this short ("-l", "/") says nothing about where it came from.
MIN_ARGUMENT_CHARS = 3

#: Which shape of lookup is being checked: a search query, a URL to fetch, a shell argv.
LookupKind = Literal["search", "fetch", "shell"]

_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}")
_WORD = re.compile(r"\w[\w.\-/:~\\]*")
_SPACE = re.compile(r"\s+")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")
#: What may stand before and after a URL the user typed, so it is matched whole.
_URL_BEFORE = r"(?:^|[\s(<\"'\[]|://|www\.)"
_URL_AFTER = r"/?(?=$|[\s)>\"'\],;!?]|\.(?:\s|$))"


def _normalise(text: str) -> str:
    """Compare like with like: percent-decoded, NFKC, case-folded, whitespace collapsed."""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", unquote(text)).casefold()).strip()


def untrusted_text(data: object, error: str = "") -> str:
    """Every string a tool result carries (keys and values), as one text for the provenance check.

    The JSON the model reads escapes quotes and newlines; collecting the leaves keeps an injected
    sentence one contiguous run.
    """
    parts: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                parts.append(str(key))
                walk(item)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)
        elif value is not None:
            parts.append(str(value))

    walk(data)
    if error:
        parts.append(error)
    return "\n".join(parts)


@dataclass(slots=True)
class _Record:
    text: str = ""
    #: Content Nox could not read as text (a provider's own web search), or more than
    #: `MAX_UNTRUSTED_CHARS`: the provenance of nothing can be shown, so nothing is user-directed.
    opaque: bool = False


@dataclass(slots=True)
class TaintLedger:
    """Which conversations hold untrusted content, what that content was, and the user's words.

    Lives in the permission engine; keyed by `PermissionRequest.conversation`.
    """

    _records: dict[str, _Record] = field(default_factory=dict)
    _user: dict[str, str] = field(default_factory=dict)

    def mark(self, conversation: str, content: str | None) -> bool:
        """Record untrusted `content` (`None` = not readable as text) in `conversation`.

        Returns True when the conversation was clean before.
        """
        if not conversation:
            return False
        fresh = conversation not in self._records
        record = self._records.setdefault(conversation, _Record())
        if content is None:
            record.opaque, record.text = True, ""
        elif not record.opaque:
            record.text += "\n" + _normalise(content)
            if len(record.text) > MAX_UNTRUSTED_CHARS:
                record.opaque, record.text = True, ""
        return fresh

    def clear(self, conversation: str) -> bool:
        """The conversation's context was discarded. Returns True if it had been tainted."""
        self._user.pop(conversation, None)
        return self._records.pop(conversation, None) is not None

    def is_tainted(self, conversation: str) -> bool:
        return bool(conversation) and conversation in self._records

    def note_user_message(self, conversation: str, text: str) -> None:
        """The user's own words for the turn now running in `conversation`."""
        if conversation:
            self._user[conversation] = _normalise(text)

    def user_directed(self, conversation: str, kind: LookupKind, value: str) -> bool:
        """Whether a lookup's free text `value` comes from the user, not from untrusted content.

        Conservative by construction: whatever it cannot show to be user-directed is not. Only
        asked for a tainted conversation (a clean one needs no exemption).
        """
        record = self._records.get(conversation)
        user = self._user.get(conversation, "")
        if record is None or record.opaque or not user:
            return False
        wanted = _normalise(value)
        if not wanted or len(wanted) > MAX_CHECKED_CHARS:
            return False
        untrusted = record.text
        if kind == "fetch":
            return _typed_url(wanted, user)
        if kind == "shell":
            words = _WORD.findall(wanted)[1:]  # the program itself is vetted (curated read-only)
            return not any(
                len(word) >= MIN_ARGUMENT_CHARS and word in untrusted and word not in user
                for word in words
            )
        for host in _HOST.findall(wanted):
            if host in untrusted and host not in user:
                return False
        for start in range(len(wanted) - COPIED_RUN_CHARS + 1):
            run = wanted[start : start + COPIED_RUN_CHARS]
            if run in untrusted and run not in user:
                return False
        return True


def _typed_url(url: str, user: str) -> bool:
    """A URL the user typed this turn, whole: scheme, `www.` and a trailing slash do not matter,
    but it must stand on its own in the message - `ample.com` is not in "example.com", nor is
    `example.com/a` in "example.com/a/b"."""
    bare = _SCHEME.sub("", url).removeprefix("www.").rstrip("/")
    if "." not in bare.split("/", 1)[0]:
        return False
    return re.search(_URL_BEFORE + re.escape(bare) + _URL_AFTER, user) is not None
