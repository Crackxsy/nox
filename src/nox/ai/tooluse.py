"""The text-sentinel tool protocol, kept as the fallback for models without native tool calling.

Claude Code calls Nox's tools natively through the MCP bridge (`nox.mcp`), and Ollama models that
declare the `tools` capability get Ollama's native `tools` field. A local model without that
capability still can ask for a tool, as text, in a shape narrow enough to be checked:

    NOX_TOOL_CALL {"name": "home.light", "arguments": {"entity_ids": ["light.desk"], "on": true}}

The Ollama provider's `SentinelSniffer` turns such a line into the same tool call the native path
produces, so nothing downstream knows which protocol was used. Three rules make it safe enough:

* **Only at the very start.** A directive is honoured only when the trimmed answer begins with the
  sentinel. A model that writes a sentence and then a directive gets neither executed nor hidden.
* **Only offered tools.** A name that was not in this turn's offer is refused before any executor
  sees it, by the tool loop, exactly as for native calls.
* **The permission engine remains the backstop.** This module decides nothing about what may run.

This module also owns the "not available right now" section and the rendering of a tool result,
which every protocol shares.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nox.ai.base import ToolDefinition
from nox.ai.prompting import untrusted

__all__ = [
    "SENTINEL",
    "OfferedTool",
    "ToolCall",
    "decided_prose",
    "parse_directive",
    "render_offer",
    "render_result",
    "summarise",
]

#: Deliberately ugly and deliberately ASCII: no German or English sentence starts with this, and
#: every model reproduces plain uppercase letters reliably.
SENTINEL = "NOX_TOOL_CALL"


class OfferedTool(BaseModel):
    """One tool as the model is told about it.

    Carries `asks_first` because "this will interrupt the user with a confirmation dialog" changes
    whether a tool is the right choice, and a model that is not told cannot weigh it.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    asks_first: bool = False


class ToolCall(BaseModel):
    """A parsed, name-checked request. Not yet permitted, not yet run."""

    model_config = ConfigDict(frozen=True)

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def _properties(schema: dict[str, Any]) -> str:
    """The argument names and types, one line, from the JSON schema the tool already publishes."""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "no arguments"
    required = set(schema.get("required") or ())
    parts = []
    for name, spec in properties.items():
        kind = spec.get("type", "any") if isinstance(spec, dict) else "any"
        parts.append(f"{name}: {kind}" + ("" if name in required else " (optional)"))
    return ", ".join(parts)


#: Descriptions are written for the dashboard, where there is room. Here every character rides
#: along on every tool-enabled turn, so the offer carries the first sentence and no more. Measured
#: at 44 tools: about 1400 tokens a turn, and roughly a fifth more without this cut.
#:
#: Offering *fewer* tools would save more and is not done, deliberately: the names are English and
#: the user writes German, so matching a request to a tool lexically misses, and a filter that
#: misses hides a capability. The way to do it is German and English trigger words per tool,
#: supplied where the tool is registered - see the budget test in tests/integration/.
SUMMARY_LIMIT = 110


def summarise(description: str) -> str:
    """The first sentence of a tool description, short enough to send on every turn."""
    first = description.strip().split(". ")[0].strip()
    if len(first) <= SUMMARY_LIMIT:
        return first
    return first[:SUMMARY_LIMIT].rsplit(" ", 1)[0] + "..."


def render_offer(tools: Sequence[ToolDefinition]) -> str:
    """The prompt section that describes the tools and the one shape a request may take.

    Empty when nothing is offered - and then the sentinel is never mentioned, so a model cannot ask
    for a tool it was not given in a turn where none was available.
    """
    if not tools:
        return ""
    lines = [
        "TOOLS",
        "You can act on this machine, but only by asking for one of the tools below.",
        f"To use one, answer with exactly one line and nothing else: {SENTINEL} "
        '{"name": "<tool>", "arguments": {...}}',
        "No greeting before it, no explanation after it - the line must be the whole answer.",
        "You will then be given the result and can answer the user normally.",
        "If no tool fits, simply answer. Never invent a tool name.",
        "",
    ]
    for tool in tools:
        note = " [asks the user for confirmation first]" if tool.asks_first else ""
        lines.append(
            f"- {tool.name}({_properties(tool.input_schema)}){note}: {summarise(tool.description)}"
        )
    return "\n".join(lines)


class Unavailable(BaseModel):
    """Something the user may ask for that cannot be done right now, and the reason in words
    the user can act on - a missing credential, a profile that forbids it, a part not built."""

    model_config = ConfigDict(frozen=True)

    #: A tool family (`calendar.*`) or the handle a missing capability would have (`file.delete`).
    name: str
    detail: str


def _bounded(detail: str, limit: int = 220) -> str:
    """One line, cut on a word; a name list is kept whole up to the limit rather than at its
    first full stop like a description."""
    flat = " ".join(detail.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + "..."


def render_unavailable(items: Sequence[Unavailable]) -> str:
    """The prompt section that names what Nox cannot do at the moment.

    Asked for something on this list, a model with only the tool offer in front of it tends to
    improvise - a confident sentence about a light it never switched. Told the reason, it can say
    the one useful thing instead: what would make it work. Empty when nothing is missing.
    """
    if not items:
        return ""
    lines = [
        "NOT AVAILABLE RIGHT NOW",
        "If the user asks for one of these, say so plainly and name what would make it work. "
        "Do not pretend it happened and do not improvise around it.",
        "",
    ]
    for item in items:
        lines.append(f"- {item.name}: {_bounded(item.detail)}")
    return "\n".join(lines)


def decided_prose(head: str) -> bool | None:
    """Is this the start of ordinary prose, a directive, or too early to tell?

    `True` prose, `False` a directive, `None` undecided. The orchestrator holds speech back until
    this answers, which costs the first few characters of latency and saves the user from hearing
    `NOX_TOOL_CALL` read aloud.
    """
    stripped = head.lstrip()
    if not stripped:
        return None if len(head) < len(SENTINEL) else True
    if SENTINEL.startswith(stripped[: len(SENTINEL)]):
        return None if len(stripped) < len(SENTINEL) else False
    return True


def parse_directive(text: str, allowed: Sequence[str]) -> ToolCall | str | None:
    """A `ToolCall`, an error to hand back to the model, or `None` when this is not a directive.

    The error is returned rather than raised because it is part of the conversation: a model that
    wrote broken JSON gets told so and tries again, which is a better turn than an exception.
    """
    stripped = text.strip()
    if not stripped.startswith(SENTINEL):
        return None
    payload = stripped[len(SENTINEL) :].strip()
    # A model that adds prose after the JSON is the common failure; take the first line only.
    payload = payload.split("\n", 1)[0].strip()
    if payload.startswith("```"):
        payload = payload.removeprefix("```json").removeprefix("```").strip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        return f"that was not valid JSON ({exc.msg}); answer with the line exactly as described"
    if not isinstance(parsed, dict):
        return "the directive must be a JSON object with 'name' and 'arguments'"
    name = parsed.get("name")
    if not isinstance(name, str) or not name:
        return "the directive needs a 'name'"
    if name not in allowed:
        return f"there is no tool {name!r} available to you right now"
    arguments = parsed.get("arguments", {})
    if not isinstance(arguments, dict):
        return "'arguments' must be a JSON object"
    return ToolCall(name=name, arguments=arguments)


def render_result(name: str, *, ok: bool, data: object = None, error: str = "") -> str:
    """The tool's outcome, as the message the model reads before answering the user.

    What a tool returns is data from outside - a file, a web page, a window title, a calendar
    entry - and may contain text written to look like an instruction. It travels in a
    `[[DATA ...]]` block, which the operating rules tell the model never to obey.
    """
    if ok:
        body = json.dumps(data, ensure_ascii=False, default=str) if data is not None else "done"
        return f"Result of {name}:\n{untrusted(body, f'tool:{name}')}"
    return f"{name} did not work: {error or 'no reason given'}"
