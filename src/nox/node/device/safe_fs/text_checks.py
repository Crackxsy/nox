"""What the text of a write may contain: JSON and YAML must parse plainly, Markdown must not hold
code a note app would run. Decided on the text alone; nothing here opens a file."""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any

from nox.node.device.safe_fs.core import OutsideFolders
from nox.node.device.safe_fs.formats import (
    MAX_STRUCTURED_BYTES,
    STRUCTURED_EXTENSIONS,
    extension_class,
    extension_of,
)

#: YAML that a loader could turn into more than data: tags (`!!python/...`, any `!tag`), and
#: anchors/aliases (the "billion laughs" expansion) - refused outright.
_YAML_ACTIVE = ("TagToken", "AnchorToken", "AliasToken")


#: Markdown a note app could run as code - refused in any `.md`, workspace or not. These are
#: position-independent tokens, not a Markdown parser: a fence inside a blockquote, a list item
#: or an unbalanced fence is still found. A note that merely mentions one of them is refused too
#: (accepted: Nox cannot write notes that mention these plugin keywords).
_MD_TOKENS = (
    "dataviewjs",
    "dataview-js",
    "js-engine",
    "templater",
    "meta-bind-js",
    "<%",
    "<script",
    "javascript:",
    "vbscript:",
    "obsidian://",
    "<iframe",
    "<object",
    "<embed",
)


#: Code-block languages that are data or plain text: a fence may name only these (or nothing).
#: Any other name could be a plugin's runnable block (Dataview JS, JS Engine, Templater,
#: Datacore, ...), a language the app renders (Mermaid, math, HTML, XML, CSS) or one a plugin
#: runs on a click (Execute Code runs Python, shells, JS, ...) - and no list of those is complete.
INERT_FENCE_LANGUAGES = frozenset(
    {
        "text",
        "plaintext",
        "txt",
        "md",
        "markdown",
        "json",
        "yaml",
        "yml",
        "toml",
        "ini",
        "csv",
        "diff",
        "log",
    }
)


#: The largest text a node writes (and the most of an existing file an append reads): every
#: check stays a few milliseconds, whatever the text.
MAX_TEXT_BYTES = 256 * 1024


#: The scan's patterns are linear without any cap that could turn into a skip: no quantifier
#: can overlap the next one, and nothing has to be given back. A fence run takes the whole run
#: of backticks/tildes greedily, the blanks after it and the whole info word (each may be empty),
#: so a match never fails or backtracks; `next` is the character right after the info word.
_FENCE_INFO = re.compile(r"(?:`{3,}|~{3,})[ \t]*\{?[ \t]*\.?([^\s`~}]*)(.?)", re.S)


#: Inline JS: a backtick or tilde, any whitespace, `$=` or `=this.` (whitespace cannot overlap
#: either side).
_INLINE_JS = re.compile(r"[`~]\s*(?:\$=|=this\.)")


#: An event handler inside a raw HTML tag; the tag's text cannot run past the next `<` or `>`,
#: so each tag is read once.
_HANDLER = re.compile(r"<[a-z][^<>]*?\bon[a-z0-9_]+\s*=")


#: The longest code-block language Nox accepts; anything longer is refused, never cut.
MAX_FENCE_LANGUAGE = 64


_FILE_LINK = re.compile(r"(?<![a-z0-9])file:")


_TEMPLATER_API = re.compile(r"\btp\.")


def markdown_finding(text: str) -> str | None:
    """What in Markdown a note app might run, in a few words - or None. After HTML entities are
    decoded, NFKC applied and case folded, with linear patterns only:

    * every run of three or more backticks or tildes, wherever it stands, names no language or
      one of `INERT_FENCE_LANGUAGES`; a backtick or tilde followed by `$=` or `=this.` is
      inline JS;
    * none of `_MD_TOKENS`, no `file:` link, no `%>` together with Templater's `tp.`;
    * no `on...=` handler inside a raw HTML tag.
    """
    body = unicodedata.normalize("NFKC", html.unescape(text)).casefold()
    for token in _MD_TOKENS:
        if token in body:
            return f"{token!r}"
    if _FILE_LINK.search(body):
        return "a file: link"
    if "%>" in body and _TEMPLATER_API.search(body):
        return "a Templater tag"
    if _INLINE_JS.search(body):
        return "inline Dataview JS"
    for fence in _FENCE_INFO.finditer(body):
        language, after = fence.group(1), fence.group(2)
        if len(language) > MAX_FENCE_LANGUAGE:
            return "an overlong code block language"
        if language and language not in INERT_FENCE_LANGUAGES:
            return f"a {language[:MAX_FENCE_LANGUAGE]!r} code block"
        if not language and after not in ("", "\n", "\r", "`", "~", "}"):
            # Something other than the end of the line follows the fence without a word
            # (a vertical tab, a form feed, ...): refused rather than guessed.
            return "an unreadable code block language"
    if _HANDLER.search(body):
        return "an HTML event handler"
    return None


def markdown_runs_code(text: str) -> bool:
    """Whether Markdown carries anything a note app might run (`markdown_finding`)."""
    return markdown_finding(text) is not None


def content_refusal(path: str, text: str) -> str | None:
    """Why the text of a write is refused even when confirmed: over `MAX_TEXT_BYTES`; JSON that
    does not parse; YAML that does not parse plainly (`safe_load`) or carries a tag, an anchor or
    an alias; Markdown with anything a note app might run. Text only, bounded and linear - it
    runs before anyone is asked (off the event loop), on the core and on the node."""
    if len(text) > MAX_TEXT_BYTES or len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        return "too_large"
    ext = extension_of(re.split(r"[\\/]", path)[-1])
    if ext in STRUCTURED_EXTENSIONS and len(text.encode("utf-8", "replace")) > MAX_STRUCTURED_BYTES:
        return "too_large"
    if ext == ".md":
        return "content_refused" if markdown_runs_code(text) else None
    if ext == ".json":
        try:
            json.loads(text)
        except (ValueError, RecursionError):
            return "invalid_content"
        return None
    if ext in (".yaml", ".yml"):
        import yaml  # noqa: PLC0415

        # The C loader where libyaml is there: the same rules, many times faster.
        loader: Any = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        try:
            for token in yaml.scan(text, Loader=loader):
                if type(token).__name__ in _YAML_ACTIVE:
                    return "content_refused"
            yaml.load(text, Loader=loader)  # noqa: S506 - a safe loader, plain or C
        except (yaml.YAMLError, RecursionError):
            return "invalid_content"
        return None
    return None


def _target_content_refusal(read: Callable[[int], bytes], size: int, target: str) -> None:
    """The write checks of the TARGET name on the file's own text, read through the verified
    handle - before anything moves (a second line of defence behind `_class_refusal`)."""
    if extension_class(target) == "passive":
        return
    if size > MAX_TEXT_BYTES:
        raise OutsideFolders("too_large", "a file this large is not renamed into a checked kind")
    text = read(MAX_TEXT_BYTES + 1).decode("utf-8", errors="replace")
    refusal = content_refusal(target, text)
    if refusal is not None:
        raise OutsideFolders(refusal, "the text would not be written under that name")
