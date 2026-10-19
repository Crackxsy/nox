"""Taint: which conversations hold content Nox did not author, and which tool results put it there.

The Security Model's "tainted turn" rule, made precise:

* **Every tool result taints, unless the tool is trusted-internal.** A shell command's stdout, a
  vault note, a memory hit, a calendar entry, a transcript, a window title, a screenshot, an MCP or
  browser result - any of it can carry text written by someone else, including instructions aimed
  at the model (prompt injection). Only tools whose results Nox composes entirely itself from its
  own state are exempt (`TRUSTED_INTERNAL_TOOLS`). An unknown tool taints.
* **Taint belongs to the conversation, not the turn.** The persistent Claude session keeps every
  earlier tool result in its context, and the orchestrator replays recent turns into every model
  call, so content read three turns ago is still "in the conversation". Taint is therefore cleared
  only when the conversation context itself is reset - a new orchestrator session - never at the
  start of the next turn.
* **Per conversation, not one global flag.** Voice, chat and Telegram turns run in parallel; a
  global flag that each new turn reset would let one turn clear another's taint. Each call carries
  its conversation key (`CONVERSATION`, set by the tool loop around the call), the executor copies
  it into the `PermissionRequest`, and the engine keeps a set of tainted keys.

What a taint changes is decided in `nox.security.permissions`: a remembered grant above LOW asks
again, a curated read-only shell command asks, and any call that sends data off the machine
(network tools, declared egress) asks - so injected text cannot make Nox exfiltrate silently.
"""

from __future__ import annotations

from contextvars import ContextVar

__all__ = ["CONVERSATION", "TRUSTED_INTERNAL_TOOLS", "current_conversation", "result_taints"]

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
