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
  without a confirmation when its free text is demonstrably taken from what the *user* said in this
  conversation (`TaintLedger.user_directed`) - a positive check, never "it copies nothing from the
  page", because injected text can split or encode what it wants to leak: every significant word
  of a `web.search` query, and every argument of a curated read-only shell command (bare option
  flags aside), is a word of the user's own messages; a `web.fetch` URL is one the user typed,
  compared after the same parsing the fetcher does, with no percent-encoding, non-ASCII or other
  form that could read differently from what is fetched. Only the user's messages count - never
  memory, replayed history, or tool output. Anything else confirms as before.

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
    "TRUSTED_INTERNAL_TOOLS",
    "LookupKind",
    "TaintLedger",
    "current_conversation",
    "result_taints",
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


#: How many of a conversation's user messages count as the source of a lookup.
MAX_USER_MESSAGES = 50
#: Free text longer than this is never checked, only confirmed.
MAX_CHECKED_CHARS = 2_048

#: Which shape of lookup is being checked: a search query, a URL to fetch, a shell argv.
LookupKind = Literal["search", "fetch", "shell"]

#: Words a query may add without the user having said them: they carry no information.
_STOPWORDS = frozenset(
    "der die das den dem des ein eine einen einem einer eines und oder in im am an auf aus bei "
    "fur fuer mit nach von vom zu zum zur uber ueber wie was wer wo wann warum welche welcher "
    "ist sind wird werden hat haben gibt es the a an of in on for to and or is are what who how "
    "when where why which with at by from".split()
)
_WORD = re.compile(r"\w+")
#: A bare option flag of a curated command (`-h`, `-la`, `--human`): no data, no host, no path.
_FLAG = re.compile(r"^--?[a-z]+(?:-[a-z]+)*$")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
#: What a URL to compare may consist of: printable ASCII without percent-escapes, backslashes,
#: userinfo or anything else two URL parsers could read differently.
_PLAIN_URL = re.compile(
    r"^(?:https?://)?[a-z0-9.-]+(?::\d{1,5})?(?:/[A-Za-z0-9._~/?&=+,;:!*$'()-]*)?$", re.I
)
_EDGE = "()<>[]\"'.,;:!?"


def _words(text: str) -> list[str]:
    """Case-folded words, umlauts spelled out - the only normalisation there is."""
    return [w.translate(_UMLAUTS) for w in _WORD.findall(text.casefold())]


def _url_key(raw: str) -> tuple[str, str] | None:
    """(host, path+query) of a plain URL, parsed the way the fetcher parses it; `None` for anything
    that is not plainly one URL (percent-escapes, non-ASCII, userinfo, a fragment, ...)."""
    if not raw or len(raw) > MAX_CHECKED_CHARS or not raw.isascii() or not _PLAIN_URL.match(raw):
        return None
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.fragment:
        return None
    host = parts.hostname.lower().removeprefix("www.")
    if "." not in host:
        return None
    port = f":{parts.port}" if parts.port else ""
    path = parts.path.rstrip("/") + (f"?{parts.query}" if parts.query else "")
    return host + port, path


@dataclass(slots=True)
class TaintLedger:
    """Which conversations hold untrusted content, and what the user said in each.

    Lives in the permission engine; keyed by `PermissionRequest.conversation`.
    """

    _tainted: set[str] = field(default_factory=set)
    _user: dict[str, list[str]] = field(default_factory=dict)

    def mark(self, conversation: str) -> bool:
        """Record untrusted content in `conversation`; True when it was clean before."""
        if not conversation or conversation in self._tainted:
            return False
        self._tainted.add(conversation)
        return True

    def clear(self, conversation: str) -> bool:
        """The conversation's context was discarded. Returns True if it had been tainted."""
        self._user.pop(conversation, None)
        if conversation in self._tainted:
            self._tainted.discard(conversation)
            return True
        return False

    def is_tainted(self, conversation: str) -> bool:
        return bool(conversation) and conversation in self._tainted

    def note_user_message(self, conversation: str, text: str) -> None:
        """What the user said in a turn of `conversation` (kept until the conversation ends)."""
        if conversation and text.strip():
            said = self._user.setdefault(conversation, [])
            said.append(text)
            del said[:-MAX_USER_MESSAGES]

    def user_directed(self, conversation: str, kind: LookupKind, value: str) -> bool:
        """Whether a lookup's free text `value` is taken from the user's own messages.

        A positive check: whatever cannot be shown to come from the user does not.
        """
        said = self._user.get(conversation, [])
        if not said or not value.strip() or len(value) > MAX_CHECKED_CHARS:
            return False
        if kind == "fetch":
            wanted = _url_key(value)
            return wanted is not None and any(
                _url_key(token.strip(_EDGE)) == wanted for text in said for token in text.split()
            )
        user_words = {word for text in said for word in _words(text)}
        if kind == "shell":
            args = value.split()[1:]  # the program itself is vetted (curated read-only)
            return all(
                _FLAG.match(arg.casefold()) or set(_words(arg)) <= user_words for arg in args
            )
        words = [word for word in _words(value) if word not in _STOPWORDS]
        return bool(words) and all(word in user_words for word in words)
