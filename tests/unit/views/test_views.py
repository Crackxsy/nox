"""What Nox may draw, and the mistakes the shapes make impossible.

The validation *is* the feature here. A model sends data into a fixed shape and the page renders it,
so the only way a wrong view reaches the screen is a shape that accepted something it should not - a
table whose rows do not line up with its columns, a line chart with more values than labels, or four
hundred rows of anything.

The wire input is flat and permissive (so the prompt can show the field names) and the strict
discriminated union sits one layer in. `_body` is that layer: it answers with the shape, or with the
sentence to hand back to the model. Both are asserted here, because the sentence is what the model
gets to act on.
"""

from __future__ import annotations

from typing import Any

import pytest

from nox.tools.registry import ToolRegistry
from nox.views.board import Board, describe
from nox.views.model import MAX_ROWS, TableView, View
from nox.views.tools import _body, register_view_tools


def table(**over: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "table",
        "title": "Offene Aufgaben",
        "columns": ["Titel", "Fällig"],
        "rows": [["Steuer", "Freitag"], ["Impfung", "Montag"]],
    }
    payload.update(over)
    return payload


def shape(payload: dict[str, Any]) -> Any:
    """The validated shape. Fails the test with the refusal if there was one."""
    body = _body(payload)
    assert not isinstance(body, str), f"expected a view, got a refusal: {body}"
    return body


def refusal(payload: dict[str, Any]) -> str:
    """The sentence handed back to the model. Fails the test if the payload was accepted."""
    body = _body(payload)
    assert isinstance(body, str), f"expected a refusal, got {body!r}"
    return body


def build(board: Board | None = None) -> tuple[dict[str, Any], Board, list[View]]:
    registry = ToolRegistry()
    # `is not None`, not `or`: Board defines __len__, so an empty one is falsy and `or` would
    # silently swap the board this test configured for a default-sized one.
    used = board if board is not None else Board()
    announced: list[View] = []

    async def announce(view: View) -> None:
        announced.append(view)

    register_view_tools(registry, used, announce)
    return {name: registry.get(name).handler for name in registry.names()}, used, announced


# ---- the shapes -------------------------------------------------------------------------------


def test_a_table_validates() -> None:
    body = shape(table())

    assert isinstance(body, TableView)
    assert body.columns == ["Titel", "Fällig"]


def test_a_row_that_does_not_fit_its_columns_is_named() -> None:
    """The mistake a model makes constantly, and the one that renders as a broken table."""
    answer = refusal(table(rows=[["a", "b"], ["a", "b", "c"]]))

    assert "row 1 has 3 cells but there are 2 columns" in answer


def test_a_table_may_be_empty_but_needs_columns() -> None:
    assert shape(table(rows=[])).rows == []
    assert refusal(table(columns=[]))


def test_too_many_rows_are_refused() -> None:
    """A view is something a person looks at. Four hundred rows is not that."""
    assert refusal(table(rows=[["a", "b"]] * (MAX_ROWS + 1)))


def test_a_line_chart_needs_one_value_per_label() -> None:
    answer = refusal(
        {
            "kind": "lines",
            "title": "Woche",
            "labels": ["Mo", "Di", "Mi"],
            "series": [{"name": "Stunden", "values": [1.0, 2.0]}],
        }
    )

    assert "has 2 values but there are 3 labels" in answer


def test_the_shape_decides_which_fields_exist() -> None:
    """A table with a `bars` list should be impossible to write down, not merely unusual."""
    assert refusal({**table(), "bars": [{"label": "x", "value": 1.0}]})


def test_an_unknown_shape_is_refused() -> None:
    assert refusal({"kind": "pie", "title": "Nope"})


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"kind": "facts", "title": "Status", "facts": [{"label": "CPU", "value": "12%"}]},
            "a list of 1 facts",
        ),
        (
            {"kind": "bars", "title": "Platz", "bars": [{"label": "C:", "value": 40.0}]},
            "a bar chart with 1 bars",
        ),
        ({"kind": "text", "title": "Notiz", "lines": ["eine Zeile"]}, "a note of 1 lines"),
    ],
)
def test_every_shape_can_describe_itself(payload: dict[str, Any], expected: str) -> None:
    """So the model can say what it drew without repeating the whole thing in its answer."""
    assert describe(shape(payload)) == expected


# ---- the board and the tool ---------------------------------------------------------------------


async def test_drawing_puts_it_on_the_board_and_announces_it() -> None:
    handlers, board, announced = build()

    answer = await handlers["view.show"](table())

    assert answer["ok"] and answer["drew"] == "a table with 2 rows"
    assert len(board) == 1 and board.latest().body.title == "Offene Aufgaben"
    assert [view.id for view in announced] == [answer["view"]]


async def test_a_broken_view_is_handed_back_as_a_sentence() -> None:
    """Mid-conversation: the model is told which field was wrong and can try again."""
    handlers, board, announced = build()

    answer = await handlers["view.show"](table(rows=[["only-one"]]))

    assert answer["ok"] is False
    assert "row 0" in answer["error"]
    assert len(board) == 0 and announced == [], "nothing is drawn and nothing is announced"


async def test_the_newest_view_is_first() -> None:
    handlers, board, _ = build()

    await handlers["view.show"](table(title="Erste"))
    await handlers["view.show"](table(title="Zweite"))

    assert [view.body.title for view in board.all()] == ["Zweite", "Erste"]


async def test_the_board_forgets_the_oldest() -> None:
    """A model in a loop must not be able to grow the process one chart at a time."""
    handlers, board, _ = build(Board(limit=3))

    for index in range(5):
        await handlers["view.show"](table(title=f"Nr {index}"))

    assert [view.body.title for view in board.all()] == ["Nr 4", "Nr 3", "Nr 2"]


async def test_the_payload_is_json_ready() -> None:
    """The dashboard reads this over the wire, so a datetime has to already be a string."""
    handlers, board, _ = build()
    await handlers["view.show"](table())

    payload = board.as_payload()

    assert isinstance(payload["views"][0]["created_at"], str)
    assert payload["views"][0]["body"]["kind"] == "table"


def test_clearing_says_how_many_went() -> None:
    board = Board()
    board.add(View(id="a", body=TableView.model_validate(table())))

    assert board.clear() == 1
    assert board.latest() is None
