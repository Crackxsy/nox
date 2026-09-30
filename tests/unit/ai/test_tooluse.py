"""The tool-use protocol, and mostly the things it must refuse.

A text protocol is only as good as its parser, and this one sits between a language model and a
machine that can start programs. So the tests that matter are the negative ones: a directive with
prose around it, a tool name nobody offered, JSON that is nearly right.
"""

from __future__ import annotations

import pytest

from nox.ai.tooluse import (
    SENTINEL,
    OfferedTool,
    ToolCall,
    decided_prose,
    parse_directive,
    render_offer,
    render_result,
)

ALLOWED = ("home.light", "time.now")


def directive(payload: str) -> str:
    return f"{SENTINEL} {payload}"


# ---- what may run ----------------------------------------------------------------------------


def test_a_well_formed_directive_is_parsed() -> None:
    parsed = parse_directive(directive('{"name": "time.now", "arguments": {}}'), ALLOWED)

    assert parsed == ToolCall(name="time.now", arguments={})


def test_arguments_survive_intact() -> None:
    parsed = parse_directive(
        directive('{"name": "home.light", "arguments": {"on": true, "pct": 30}}'), ALLOWED
    )

    assert isinstance(parsed, ToolCall)
    assert parsed.arguments == {"on": True, "pct": 30}


def test_a_code_fence_is_tolerated() -> None:
    """Models wrap JSON in fences out of habit - a formatting tic, not a different request."""
    parsed = parse_directive(directive('```json {"name": "time.now"}'), ALLOWED)

    assert isinstance(parsed, ToolCall) and parsed.name == "time.now"


# ---- what must not run -----------------------------------------------------------------------


def test_an_ordinary_answer_is_not_a_directive() -> None:
    assert parse_directive("Das Licht ist schon an.", ALLOWED) is None


def test_a_directive_with_a_sentence_in_front_of_it_is_ignored() -> None:
    """The case that matters: prose plus a directive must not quietly run a tool."""
    text = f"Einen Moment. {SENTINEL} " + '{"name": "home.light", "arguments": {}}'

    assert parse_directive(text, ALLOWED) is None


def test_a_tool_that_was_not_offered_is_refused() -> None:
    answer = parse_directive(directive('{"name": "file.delete", "arguments": {}}'), ALLOWED)

    assert isinstance(answer, str)
    assert "file.delete" in answer


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("{not json}", "valid JSON"),
        ('["home.light"]', "JSON object"),
        ('{"arguments": {}}', "needs a 'name'"),
        ('{"name": "home.light", "arguments": "on"}', "must be a JSON object"),
    ],
)
def test_a_broken_directive_comes_back_as_an_answer_not_an_exception(
    payload: str, expected: str
) -> None:
    """The model is mid-conversation: it gets told what was wrong and can try once more."""
    answer = parse_directive(directive(payload), ALLOWED)

    assert isinstance(answer, str) and expected in answer


# ---- holding speech back ---------------------------------------------------------------------


@pytest.mark.parametrize("head", ["Hallo", "Das Licht", "N ", "nox_tool_call"])
def test_prose_is_released_as_soon_as_it_cannot_be_the_sentinel(head: str) -> None:
    assert decided_prose(head) is True


@pytest.mark.parametrize("head", ["N", "NOX", "NOX_TOOL", " NOX_TOOL_CAL"])
def test_an_ambiguous_head_is_held(head: str) -> None:
    assert decided_prose(head) is None


def test_the_sentinel_is_recognised_as_soon_as_it_is_complete() -> None:
    assert decided_prose(SENTINEL) is False
    assert decided_prose(f"  {SENTINEL} " + '{"name"') is False


def test_an_empty_head_is_held_only_briefly() -> None:
    """A stream that begins with whitespace must not hold forever."""
    assert decided_prose("") is None
    assert decided_prose(" " * len(SENTINEL)) is True


# ---- what the model is told -------------------------------------------------------------------


def test_nothing_offered_means_the_protocol_is_never_mentioned() -> None:
    """A model told about a protocol it cannot use will eventually try to use it."""
    assert render_offer([]) == ""


def test_the_offer_names_the_arguments_and_the_confirmation() -> None:
    offer = render_offer(
        [
            OfferedTool(
                name="home.light",
                description="Dim the lights.",
                schema={
                    "properties": {"entity_ids": {"type": "array"}, "pct": {"type": "integer"}},
                    "required": ["entity_ids"],
                },
                asks_first=True,
            )
        ]
    )

    assert SENTINEL in offer
    assert "entity_ids: array" in offer
    assert "pct: integer (optional)" in offer
    assert "asks the user for confirmation" in offer


def test_a_result_reads_as_a_sentence() -> None:
    assert "done" in render_result("time.now", ok=True)
    assert "no token" in render_result("home.light", ok=False, error="no token stored")
