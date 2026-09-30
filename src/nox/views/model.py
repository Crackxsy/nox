"""What Nox may draw: five shapes, filled with data, and no code.

The tempting design is to let the model write HTML or SVG and put it on the page. That is an
injection hole with extra steps: the dashboard would be rendering whatever a language model was
talked into producing. So the model never sends markup. It picks one of five shapes and fills in
values, and the page owns the rendering entirely.

The shapes are a discriminated union rather than one model with every field on it, because a table
with a `bars` list is a bug that should be impossible to write down, not a convention to remember.
Every list is bounded: a view is something a person looks at, and four hundred rows is not that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "Bar",
    "BarsView",
    "Fact",
    "FactsView",
    "LinesView",
    "Series",
    "TableView",
    "TextView",
    "View",
    "ViewBody",
]

MAX_ROWS = 100
MAX_BARS = 40
MAX_POINTS = 60
MAX_SERIES = 4


class _Shape(BaseModel):
    """Everything every view has: a title, and one line saying what it shows.

    `extra="forbid"` is the point of the discriminated union, not a detail of it. Pydantic's default
    would quietly drop a `bars` list sent with a table, and the model would see a table with no bars
    and no explanation for where they went. Forbidding it turns that into a sentence it can act on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=80)
    #: One sentence under the title. Where the model explains the picture, so the picture does not
    #: have to explain itself.
    note: str = Field(default="", max_length=200)


class Fact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str = Field(min_length=1, max_length=60)
    value: str = Field(max_length=120)


class Bar(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    label: str = Field(min_length=1, max_length=60)
    value: float


class Series(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1, max_length=60)
    values: list[float] = Field(min_length=1, max_length=MAX_POINTS)


class TableView(_Shape):
    """Rows and columns. Everything is a string: a table is for reading, not for arithmetic."""

    kind: Literal["table"] = "table"
    columns: list[str] = Field(min_length=1, max_length=8)
    rows: list[list[str]] = Field(default_factory=list, max_length=MAX_ROWS)

    @model_validator(mode="after")
    def _rows_match_columns(self) -> TableView:
        width = len(self.columns)
        for index, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(f"row {index} has {len(row)} cells but there are {width} columns")
        return self


class BarsView(_Shape):
    """One value per label. For "how much of what" - disk usage, time per project."""

    kind: Literal["bars"] = "bars"
    bars: list[Bar] = Field(min_length=1, max_length=MAX_BARS)
    #: What the numbers are, for the axis: "GB", "minutes", "€".
    unit: str = Field(default="", max_length=16)


class LinesView(_Shape):
    """Values over a shared set of labels. For "how it changed"."""

    kind: Literal["lines"] = "lines"
    labels: list[str] = Field(min_length=1, max_length=MAX_POINTS)
    series: list[Series] = Field(min_length=1, max_length=MAX_SERIES)
    unit: str = Field(default="", max_length=16)

    @model_validator(mode="after")
    def _series_match_labels(self) -> LinesView:
        expected = len(self.labels)
        for series in self.series:
            if len(series.values) != expected:
                raise ValueError(
                    f"series {series.name!r} has {len(series.values)} values "
                    f"but there are {expected} labels"
                )
        return self


class FactsView(_Shape):
    """Label and value pairs. The shape most answers actually want."""

    kind: Literal["facts"] = "facts"
    facts: list[Fact] = Field(min_length=1, max_length=20)


class TextView(_Shape):
    """Plain lines. No markup: the page renders them as text, and React escapes them."""

    kind: Literal["text"] = "text"
    lines: list[str] = Field(min_length=1, max_length=40)


ViewBody = Annotated[
    TableView | BarsView | LinesView | FactsView | TextView,
    Field(discriminator="kind"),
]


class View(BaseModel):
    """One drawn view, as the board keeps it and the dashboard reads it."""

    model_config = ConfigDict(frozen=True)

    id: str
    body: ViewBody
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
