"""`filter_outbound`: what a chat line has to look like before Nox may send it (Stream Bot).

Runs on *Nox's own* text, right before the send, for every path alike - the language-model answer,
the built-in command replies and the core's `!funken` / disclosure lines all reach Twitch through
`TwitchPlugin._send`. It is deterministic and sits below the model: a prompt-injected or merely
careless answer cannot talk its way around it. It is not a content filter; `ModerationGate` still
judges what is *said*. This module decides what a line may *do* in Twitch chat:

- Invisible characters (zero-width and other format characters, control characters, line
  separators, variation selectors) are removed and runs of white space collapse to one space, so
  nothing can hide inside a word or a link.
- Links are refused: schemes, `www.`, anything shaped like a domain (a word, a dot, two letters,
  no space - also with spaces around the dot before a common ending, or spelled "dot com"), IP
  addresses and punycode (`xn--`). Detection works
  on a folded copy - full-width forms, accents and look-alike Cyrillic/Greek letters are reduced to
  plain Latin - so a link typed in full-width letters or with a Cyrillic "a" is caught like the
  plain one.
- A line may not start with `/` or `.` (Twitch chat commands such as `/ban` or `.timeout`), `!`
  (another bot's command) or a backslash, however it is spelled or padded. No path of Nox's own
  needs a leading `!`: his command answers are plain sentences.
- `@everyone`-style mass mentions lose their `@`, and a line pings at most `MAX_MENTIONS` people.
  Only an `@` that starts a word is a mention (`name@host` and `end.@name` are not).
- A line is cut to `MAX_OUTBOUND_CHARS` characters.
- The link and command-prefix rules are applied twice: to the cleaned line and again to the text
  that would actually be sent (after the mention cap and the cut), because removing an `@` can
  glue a word to what follows it ("foo dot @com" becomes "foo dot com").

A refused line is not repaired or replaced: nothing is sent, and the caller reports why. The reason
is a short code, never the text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: Twitch's own limit is 500 characters; this stays clear of it.
MAX_OUTBOUND_CHARS = 450
#: At most this many `@name` mentions survive in one line.
MAX_MENTIONS = 2

#: Reasons a line is refused (`OutboundResult.blocked`).
BLOCKED_EMPTY = "empty"
BLOCKED_COMMAND_PREFIX = "command_prefix"
BLOCKED_LINK = "link"

#: First characters that make Twitch (`/`, `.`) or another bot (`!`) treat a line as a command.
_COMMAND_PREFIXES = "/.!\\"

#: Top-level domains that are never ordinary words. Only used where a domain is written with
#: spaces ("name . com", "name dot com"); a domain written together is caught for any ending.
_PLAIN_TLDS = (
    "com|net|org|edu|gov|info|biz|xyz|gg|tv|ly|io|tk|cc|ru|cn|vip|top|club|shop|site|online|"
    "live|fun|link|app|dev|page|store|tech|cloud|download|stream|games|chat|blog"
)

_BOUNDARY_BEFORE = r"(?<![^\W_])"
_BOUNDARY_AFTER = r"(?![^\W_])"
_LINK_PATTERNS = (
    # https://x, ftp://x, "https :", "s3://x"
    re.compile(r"(?:\b(?:https?|ftps?|wss?)\s*:|[a-z0-9]\s*:\s*/\s*/)"),
    re.compile(rf"{_BOUNDARY_BEFORE}www\s*\.\s*[^\W_]"),
    # name.anything, a.b.name.anything: a word, a dot and at least two letters, no space between.
    # Any ending counts (there are over a thousand), so a missing space after a full stop
    # ("Danke.Tschüss") reads as a domain too; digits on the right ("19.30") never do.
    re.compile(
        rf"{_BOUNDARY_BEFORE}(?=[\w-]*[^\W\d_])[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{{2,}}{_BOUNDARY_AFTER}"
    ),
    # name . com, name .com, name. com
    re.compile(rf"{_BOUNDARY_BEFORE}[\w-]+\s*\.\s*(?:{_PLAIN_TLDS}){_BOUNDARY_AFTER}"),
    # name dot com, name (dot) com, name punkt de
    re.compile(
        rf"{_BOUNDARY_BEFORE}[\w-]+\s*[(\[{{]?\s*(?:dot|punkt)\s*[)\]}}]?\s*"
        rf"(?:{_PLAIN_TLDS}){_BOUNDARY_AFTER}"
    ),
    # 203.0.113.7, also with spaces around the dots
    re.compile(r"(?<![\d.])\d{1,3}(?:\s*\.\s*\d{1,3}){3}(?!\d)"),
    # xn--80ak6aa92e
    re.compile(r"xn--[a-z0-9-]+"),
)

#: Letters of other scripts that look like a Latin one (the usual suspects), written as code
#: points so the source itself holds no look-alike characters. Folded for detection only.
_LOOKALIKES = str.maketrans(
    {
        "\u0430": "a",  # Cyrillic a
        "\u0435": "e",  # Cyrillic e
        "\u043e": "o",  # Cyrillic o
        "\u0440": "p",  # Cyrillic er
        "\u0441": "c",  # Cyrillic es
        "\u0443": "y",  # Cyrillic u
        "\u0445": "x",  # Cyrillic ha
        "\u0456": "i",  # Cyrillic Ukrainian i
        "\u0458": "j",  # Cyrillic je
        "\u0455": "s",  # Cyrillic dze
        "\u04bb": "h",  # Cyrillic shha
        "\u0501": "d",  # Cyrillic komi de
        "\u051b": "q",  # Cyrillic qa
        "\u0261": "g",  # Latin script g
        "\u03bf": "o",  # Greek omicron
        "\u03bd": "v",  # Greek nu
        "\u03b9": "i",  # Greek iota
        "\u03ba": "k",  # Greek kappa
        "\u3002": ".",  # ideographic full stop
        "\uff61": ".",  # half-width ideographic full stop
    }
)

#: An `@` starts a mention only at the start of a word: not inside "name@host" and not right after
#: a dot ("end.@name"), where Twitch pings nobody and where dropping the `@` would glue two words.
_MENTION_RE = re.compile(r"(?<![\w.])[@\uff20\ufe6b](\w{1,25})")
_MASS_MENTIONS = frozenset({"everyone", "here", "channel", "all"})


@dataclass(frozen=True, slots=True)
class OutboundResult:
    """`text` is what may be sent ("" when `blocked`); `changes` names what was cleaned up."""

    text: str
    blocked: str = ""
    changes: tuple[str, ...] = ()


def filter_outbound(text: str) -> OutboundResult:
    """Clean `text` for chat or refuse it; see the module docstring for the rules."""
    cleaned = _collapse_white_space(_drop_invisible(text))
    changes: list[str] = []
    if cleaned != text.strip():
        changes.append("invisible_or_white_space")
    if not cleaned:
        return OutboundResult("", BLOCKED_EMPTY)

    reason = _refusal(cleaned)
    if reason:
        return OutboundResult("", reason)

    capped = _cap_mentions(cleaned)
    if capped != cleaned:
        changes.append("mentions_capped")
        cleaned = capped
    if len(cleaned) > MAX_OUTBOUND_CHARS:
        cleaned = cleaned[: MAX_OUTBOUND_CHARS - 1].rstrip() + "\u2026"
        changes.append("truncated")

    # The rules judge the line that would go out, not only the one that came in.
    reason = _refusal(cleaned)
    if reason:
        return OutboundResult("", reason)
    return OutboundResult(cleaned, changes=tuple(changes))


def _refusal(text: str) -> str:
    """The reason `text` may not be sent, or "" when it may (empty, command prefix, link)."""
    probe = _fold(text)
    if not probe:
        return BLOCKED_EMPTY
    if probe[0] in _COMMAND_PREFIXES:
        return BLOCKED_COMMAND_PREFIX
    if any(pattern.search(probe) for pattern in _LINK_PATTERNS):
        return BLOCKED_LINK
    return ""


def _drop_invisible(text: str) -> str:
    """Remove format characters (zero-width, bidi, tags, soft hyphen) and variation selectors;
    control characters and line/paragraph separators become a space."""
    out: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category == "Cf" or 0xFE00 <= ord(ch) <= 0xFE0F or 0xE0100 <= ord(ch) <= 0xE01EF:
            continue
        out.append(" " if category in ("Cc", "Zl", "Zp") else ch)
    return "".join(out)


def _collapse_white_space(text: str) -> str:
    return " ".join(text.split())


def matching_variants(text: str) -> tuple[str, ...]:
    """The forms of `text` a word list is matched against, for a checker that gets the text as it
    is written (the moderation gate): lower-cased as is; composed (NFKC: full-width letters and
    ligatures plain, a letter plus a combining mark one letter, so umlauts in a list still match);
    and folded (see `_fold`: accents gone, look-alike letters Latin). Invisible characters are
    removed from the last two."""
    visible = _drop_invisible(text)
    forms = (text.lower(), unicodedata.normalize("NFKC", visible).lower(), _fold(visible))
    return tuple(dict.fromkeys(forms))


def _fold(text: str) -> str:
    """A copy for detection only: compatibility forms (full-width, ligatures) become plain
    characters, accents and other combining marks go, look-alike letters turn Latin."""
    decomposed = unicodedata.normalize("NFKD", text)
    unmarked = "".join(ch for ch in decomposed if not unicodedata.category(ch).startswith("M"))
    return unmarked.casefold().translate(_LOOKALIKES)


def _cap_mentions(text: str) -> str:
    """Drop the `@` of a mass mention and of every mention past `MAX_MENTIONS`."""
    seen = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal seen
        name = match.group(1)
        if _fold(name) in _MASS_MENTIONS:
            return name
        seen += 1
        return match.group(0) if seen <= MAX_MENTIONS else name

    return _MENTION_RE.sub(_replace, text)
