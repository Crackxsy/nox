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
  without a confirmation when its free text is demonstrably taken from what the *user* said
  (`TaintLedger.user_directed`). Two conditions, both required:
  1. Positive: a `web.search` query is plain words that form a subsequence - same order, no word
     twice, at most `MAX_QUERY_WORDS` words / `MAX_QUERY_CHARS` characters - of *one* of the user's
     messages in this conversation, so neither choice nor order nor repetition can encode anything
     else; a `web.fetch` URL is one the user typed, compared after the fetcher's own parsing (same
     scheme or an http->https upgrade, the same ASCII host, the same path and query byte for
     byte; percent-escapes, non-ASCII, userinfo and fragments never match); every argument of a
     curated read-only shell command is a word of the user's messages or a bare option flag.
  2. Negative: the free text copies no run of `COPIED_RUN_CHARS` characters out of untrusted
     content that the user did not also write, and nothing in the conversation was unreadable
     (`mark(..., None)`: the provider's own web search).
  Only the user's messages count - never memory, replayed history or tool output. Anything else
  confirms as before.

What a taint changes is decided in `nox.security.permissions`: a remembered grant above LOW asks
again, a curated read-only shell command asks, and any call that sends data off the machine
(network tools, declared egress) asks - so injected text cannot make Nox exfiltrate silently.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

__all__ = [
    "CONVERSATION",
    "UNTRUSTED_CONVERSATIONS",
    "COPIED_RUN_CHARS",
    "MAX_UNTRUSTED_CHARS",
    "TRUSTED_INTERNAL_TOOLS",
    "LookupKind",
    "TaintLedger",
    "current_conversation",
    "result_taints",
    "untrusted_text",
]

#: Conversations whose every message comes from a source Nox cannot vouch for, tainted from their
#: first turn and never cleared (C9): what a node's voice satellite heard - a far-field microphone
#: in a room, not necessarily the owner. As if `mark(conversation, None)` had been called: nothing
#: in them is user-directed, a remembered grant above LOW asks again, and every call that sends
#: data off the machine asks.
UNTRUSTED_CONVERSATIONS: tuple[str, ...] = ("voice:node:",)

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


#: How many of a conversation's user messages count as the source of a lookup.
MAX_USER_MESSAGES = 50
#: Free text longer than this is never checked, only confirmed.
MAX_CHECKED_CHARS = 2_048
#: A user-directed search query is at most this long.
MAX_QUERY_WORDS = 12
MAX_QUERY_CHARS = 200
#: A run of this many characters copied from untrusted content makes a lookup not user-directed.
COPIED_RUN_CHARS = 20
#: Untrusted text kept per conversation for that check. A conversation that read more is opaque:
#: nothing in it counts as user-directed any more (it confirms, as before).
MAX_UNTRUSTED_CHARS = 512_000

#: Which shape of lookup is being checked: a search query, a URL to fetch, a shell argv.
LookupKind = Literal["search", "fetch", "shell"]

_WORD = re.compile(r"\w+")
#: A plain query: words separated by single spaces, nothing else.
_PLAIN_QUERY = re.compile(r"\w+(?: \w+)*")
#: A bare option flag of a curated command (`-h`, `-la`, `--human`): no data, no host, no path.
_FLAG = re.compile(r"^--?[a-z]+(?:-[a-z]+)*$")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
#: What a URL to compare may consist of: printable ASCII without percent-escapes, backslashes,
#: userinfo or anything else two URL parsers could read differently.
_PLAIN_URL = re.compile(
    r"^(?:https?://)?[a-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/?&=+,;:!*$'()-]*)?$", re.I
)
_EDGE = "()<>[]\"'.,;:!?"
_SPACE = re.compile(r"\s+")


def _words(text: str) -> list[str]:
    """Case-folded words, umlauts spelled out - the only normalisation there is."""
    return [w.translate(_UMLAUTS) for w in _WORD.findall(text.casefold())]


def _flat(text: str) -> str:
    """For the copied-run check: case-folded, umlauts spelled out, whitespace collapsed."""
    return _SPACE.sub(" ", text.casefold().translate(_UMLAUTS)).strip()


def _subsequence(wanted: list[str], words: list[str]) -> bool:
    remaining = iter(words)
    return all(word in remaining for word in wanted)


def _url_key(raw: str, *, typed: bool = False) -> tuple[str, str, str] | None:
    """(scheme, host[:port], path?query) of a plain URL, parsed the way the fetcher parses it;
    `None` for anything that is not plainly one URL (percent-escapes, non-ASCII, userinfo, a
    fragment, ...). A URL the user `typed` without a scheme gets the scheme "" (http or https)."""
    if not raw or len(raw) > MAX_CHECKED_CHARS or not raw.isascii() or not _PLAIN_URL.match(raw):
        return None
    if "://" not in raw and not typed:
        return None
    parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    scheme = parts.scheme.lower() if "://" in raw else ""
    if scheme not in ("", "http", "https") or not parts.hostname or parts.fragment:
        return None
    if "." not in parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return scheme, parts.hostname + port, path


def _same_url(fetched: tuple[str, str, str], typed: tuple[str, str, str]) -> bool:
    """Host and path byte for byte; the scheme exactly, or the user's http (or no scheme)
    upgraded to https - never https downgraded."""
    scheme, host, path = fetched
    typed_scheme, typed_host, typed_path = typed
    if (host, path) != (typed_host, typed_path):
        return False
    return (
        scheme == typed_scheme
        or (scheme == "https" and typed_scheme in ("", "http"))
        or (scheme == "http" and typed_scheme == "")
    )


def untrusted_text(data: object, error: str = "") -> str:
    """Every string a tool result carries (keys and values), as one text for the copied-run check.

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
    #: Content Nox could not read as text (the provider's own web search), or more than
    #: `MAX_UNTRUSTED_CHARS`: nothing in the conversation counts as user-directed any more.
    opaque: bool = False


@dataclass(slots=True)
class TaintLedger:
    """Which conversations hold untrusted content, what it was, and what the user said in each.

    Lives in the permission engine; keyed by `PermissionRequest.conversation`.
    """

    _records: dict[str, _Record] = field(default_factory=dict)
    _user: dict[str, list[str]] = field(default_factory=dict)

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
            record.text += "\n" + _flat(content)
            if len(record.text) > MAX_UNTRUSTED_CHARS:
                record.opaque, record.text = True, ""
        return fresh

    def clear(self, conversation: str) -> bool:
        """The conversation's context was discarded. Returns True if it had been tainted."""
        self._user.pop(conversation, None)
        return self._records.pop(conversation, None) is not None

    def is_tainted(self, conversation: str) -> bool:
        if not conversation:
            return False
        return conversation in self._records or conversation.startswith(UNTRUSTED_CONVERSATIONS)

    def note_user_message(self, conversation: str, text: str) -> None:
        """What the user said in a turn of `conversation` (kept until the conversation ends)."""
        if conversation and text.strip():
            said = self._user.setdefault(conversation, [])
            said.append(text)
            del said[:-MAX_USER_MESSAGES]

    def user_directed(self, conversation: str, kind: LookupKind, value: str) -> bool:
        """Whether a lookup's free text `value` is taken from the user's own messages (and not
        copied from untrusted content). Whatever cannot be shown to come from the user does not."""
        record = self._records.get(conversation)
        said = self._user.get(conversation, [])
        if conversation.startswith(UNTRUSTED_CONVERSATIONS):
            return False  # not the owner's words for sure: nothing is user-directed
        if record is None or record.opaque or not said or not value.strip():
            return False
        if len(value) > MAX_CHECKED_CHARS or _copies_untrusted(value, record.text, said):
            return False
        if kind == "fetch":
            fetched = _url_key(value)
            if fetched is None:
                return False
            typed = (_url_key(t.strip(_EDGE), typed=True) for m in said for t in m.split())
            return any(key is not None and _same_url(fetched, key) for key in typed)
        if kind == "shell":
            user_words = {word for message in said for word in _words(message)}
            args = value.split()[1:]  # the program itself is vetted (curated read-only)
            return all(
                _FLAG.match(arg.casefold()) or set(_words(arg)) <= user_words for arg in args
            )
        query = value.strip()
        words = _words(query)
        if (
            len(query) > MAX_QUERY_CHARS
            or not _PLAIN_QUERY.fullmatch(query)
            or len(words) > MAX_QUERY_WORDS
            or len(set(words)) != len(words)
        ):
            return False
        return any(_subsequence(words, _words(message)) for message in said)


def _copies_untrusted(value: str, untrusted: str, said: list[str]) -> bool:
    """Whether `value` contains a run of `COPIED_RUN_CHARS` characters that is in untrusted
    content and in none of the user's messages."""
    wanted = _flat(value)
    user = [_flat(message) for message in said]
    for start in range(len(wanted) - COPIED_RUN_CHARS + 1):
        run = wanted[start : start + COPIED_RUN_CHARS]
        if run in untrusted and not any(run in message for message in user):
            return True
    return False
