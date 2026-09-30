"""How a model asks for a tool, and the guards around that request.

Neither provider Nox uses can be given tools natively today. The Claude Code CLI is started with
``--tools ""`` on purpose - its own Read/Write/Bash tools would run outside Nox's permission engine
entirely, which is the one thing that must never happen - and the local Ollama models Nox falls back
to may or may not support tool calling depending on which model is pulled. So the request travels as
text, in a shape narrow enough to be checked:

    NOX_TOOL_CALL {"name": "home.light", "arguments": {"entity_ids": ["light.desk"], "on": true}}

Three rules make that safe enough to act on:

* **Only at the very start.** A directive is honoured only when the trimmed answer begins with the
  sentinel. A model that writes a sentence and then a directive gets neither executed nor hidden -
  it gets logged, because a silently swallowed tool call is the kind of bug nobody finds.
* **Only offered tools.** A name that was not in this turn's offer is refused before any executor
  sees it. The offer itself comes from the capability report, so a tool the active profile denies is
  never even mentioned to the model.
* **The permission engine remains the backstop.** This module decides nothing about what may run.
  Memory context is injected into the same prompt, and a vault note could contain a line that looks
  like a directive - so the answer to "what if the model is talked into it" is not this parser, it
  is that `presets.run_action` still needs a confirmation and a door lock still has no tool at all.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


def _properties(tool: OfferedTool) -> str:
    """The argument names and types, one line, from the JSON schema the tool already publishes."""
    properties = tool.schema_.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "no arguments"
    required = set(tool.schema_.get("required") or ())
    parts = []
    for name, spec in properties.items():
        kind = spec.get("type", "any") if isinstance(spec, dict) else "any"
        parts.append(f"{name}: {kind}" + ("" if name in required else " (optional)"))
    return ", ".join(parts)


#: Descriptions are written for the dashboard, where there is room. Here every character rides
#: along on every tool-enabled turn, so the offer carries the first sentence and no more. Measured:
#: the full text of 26 tools costs about 1100 tokens a turn, the first sentences about half that.
SUMMARY_LIMIT = 110


def summarise(description: str) -> str:
    """The first sentence of a tool description, short enough to send on every turn."""
    first = description.strip().split(". ")[0].strip()
    if len(first) <= SUMMARY_LIMIT:
        return first
    return first[:SUMMARY_LIMIT].rsplit(" ", 1)[0] + "..."


def render_offer(tools: Sequence[OfferedTool]) -> str:
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
        lines.append(f"- {tool.name}({_properties(tool)}){note}: {summarise(tool.description)}")
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
    """The tool's outcome, as the message the model reads before answering the user."""
    if ok:
        body = json.dumps(data, ensure_ascii=False, default=str) if data is not None else "done"
        return f"Result of {name}: {body}"
    return f"{name} did not work: {error or 'no reason given'}"
