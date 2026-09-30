"""`Board`: the views Nox has drawn, newest first.

In memory and bounded, deliberately. A view is the visual half of an answer in a conversation, and a
conversation is not a document: keeping them across restarts would mean booting into a wall of
charts nobody asked to see again. What is worth keeping goes in the vault, which is what the vault
is for.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from typing import Any

from nox.core.logging import get_logger
from nox.views.model import (
    BarsView,
    FactsView,
    LinesView,
    TableView,
    TextView,
    View,
    ViewBody,
)

log = get_logger(__name__)

__all__ = ["Board", "MAX_VIEWS"]

#: Enough to scroll back through a conversation, small enough that a model in a loop cannot grow
#: the process one chart at a time.
MAX_VIEWS = 20


class Board:
    def __init__(self, limit: int = MAX_VIEWS) -> None:
        self._views: deque[View] = deque(maxlen=limit)

    def add(self, view: View) -> View:
        self._views.appendleft(view)
        log.info("views.shown", view=view.id, kind=view.body.kind, title=view.body.title)
        return view

    def latest(self) -> View | None:
        return self._views[0] if self._views else None

    def get(self, view_id: str) -> View | None:
        return next((view for view in self._views if view.id == view_id), None)

    def all(self) -> list[View]:
        return list(self._views)

    def clear(self) -> int:
        count = len(self._views)
        self._views.clear()
        return count

    def as_payload(self) -> dict[str, Any]:
        """What the dashboard reads: every view in full, newest first."""
        return {"views": [view.model_dump(mode="json") for view in self._views]}

    def __len__(self) -> int:
        return len(self._views)

    def __iter__(self) -> Iterator[View]:
        return iter(self._views)


def describe(body: ViewBody) -> str:
    """One line for the model's own answer, so it can say what it drew without repeating it."""
    match body:
        case TableView():
            return f"a table with {len(body.rows)} rows"
        case BarsView():
            return f"a bar chart with {len(body.bars)} bars"
        case LinesView():
            return f"a line chart with {len(body.series)} lines over {len(body.labels)} points"
        case FactsView():
            return f"a list of {len(body.facts)} facts"
        case TextView():
            return f"a note of {len(body.lines)} lines"
