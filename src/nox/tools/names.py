"""Tool names on the wire: Nox's dotted names mapped to what tool-calling APIs accept.

Anthropic, MCP and Ollama tool names allow letters, digits, `_` and `-`, but no dot. Nox names its
tools `area.verb` (`file.read`, `obs.scene.switch`), so the dot becomes `-` on the way out and back
on the way in. The mapping is reversible only because no registered name contains `-`, which
`ToolRegistry.register` enforces.
"""

from __future__ import annotations

import re

__all__ = ["WIRE_NAME_RE", "from_wire", "to_wire"]

#: What every provider accepts (the strictest common subset, at most 64 characters).
WIRE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def to_wire(name: str) -> str:
    """`desktop.type_text` -> `desktop-type_text`."""
    wire = name.replace(".", "-")
    if not WIRE_NAME_RE.fullmatch(wire):
        raise ValueError(f"tool name {name!r} cannot be expressed as a wire name")
    return wire


def from_wire(wire: str) -> str:
    """`desktop-type_text` -> `desktop.type_text`. Never raises: an unknown name is refused later
    by the offer check, which is where a model's invention belongs."""
    return wire.replace("-", ".")
