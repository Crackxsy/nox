"""`view.show`: the one tool that draws something.

The input is **flat** - `kind`, `title` and the fields that shape needs - and that is a decision
about what the model can actually see. The tool offer in the prompt lists argument names and types,
not nested schemas, so a single `view` object would have appeared as `view: object` and the five
shapes would have been invisible. Flat, every field shows up by name.

The strictness lives one layer in. The flat input is validated against the discriminated union in
`nox.views.model`, which is where a table with a `bars` list is refused and a row that does not fit
its columns is named. That also buys a usable error: the executor answers a failed input validation
with a generic "invalid input" on purpose - it must not put user content in a log - so a tool whose
shape is fiddly has to do its own checking to be able to say *which* field was wrong.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from nox.core.logging import get_logger
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec
from nox.views.board import Board, describe
from nox.views.model import Bar, Fact, Series, View, ViewBody

log = get_logger(__name__)

__all__ = ["ShowInput", "register_view_tools"]

Announce = Callable[[View], Awaitable[None]]

_BODY: TypeAdapter[ViewBody] = TypeAdapter(ViewBody)

DESCRIPTION = (
    "Draw something on the dashboard's Board page. Pick a kind and fill in what it needs: "
    "table (columns, rows), bars (bars, unit), lines (labels, series, unit), facts (facts), "
    "text (lines). Use it when numbers or a comparison are clearer looked at than read out."
)


class ShowInput(BaseModel):
    """Every field any shape can carry. Which ones apply is decided by `kind`.

    Permissive here and strict one layer in: the point of this model is that the prompt can show the
    field names. `nox.views.model` is what refuses the wrong ones.
    """

    kind: Literal["table", "bars", "lines", "facts", "text"]
    title: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=200)
    columns: list[str] | None = None
    rows: list[list[str]] | None = None
    bars: list[Bar] | None = None
    labels: list[str] | None = None
    series: list[Series] | None = None
    facts: list[Fact] | None = None
    lines: list[str] | None = None
    unit: str | None = None


def _body(payload: dict[str, Any]) -> ViewBody | str:
    """The validated shape, or the sentence to hand back to the model.

    Keys that were not given are dropped before validating, so `extra="forbid"` on the shapes
    catches a field that belongs to a different kind - a table sent with `bars` - instead of every
    unused field tripping it.
    """
    given = {key: value for key, value in payload.items() if value is not None}
    try:
        return _BODY.validate_python(given)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"] if part != payload.get("kind"))
        return f"{where or 'view'}: {first['msg']}"


def register_view_tools(registry: ToolRegistry, board: Board, announce: Announce) -> None:
    """Register `view.show`."""

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        body = _body(payload)
        if isinstance(body, str):
            log.info("views.refused", reason=body)
            return {"ok": False, "error": body}
        view = board.add(View(id=uuid4().hex[:12], body=body))
        await announce(view)
        return {
            "ok": True,
            "view": view.id,
            "drew": describe(body),
            "note": "it is on the Board page in the dashboard",
        }

    registry.register(
        ToolSpec(
            name="view.show",
            description=DESCRIPTION,
            input_model=ShowInput,
            risk=Risk.LOW,
            side_effects=True,
            local=True,
            handler=handler,
        )
    )
    log.info("views.tools_registered", count=1)
