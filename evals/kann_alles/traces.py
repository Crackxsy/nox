"""Recorded traces: a task's tool calls and answer, replayed without a model and without the judge.

A live run decides with a model what to call and with the judge what the answer claims. Both are
the expensive, non-deterministic parts, so neither can run in CI. A trace keeps exactly those two
decisions of one run - the ordered tool calls the model made (name and arguments, `device`
included), the answer it gave, and the judge's verdict on that answer - and a replay feeds them
back into a real core:

* every recorded call goes through `ToolLoop.execute` of a fresh turn, so it meets this turn's
  offer, its budget, the executor, the permission engine (whose dialogs the harness answers as in
  a live run), the router, the signed receipt, the device's own `NodeGate` and the node tools;
* the deterministic checks then look at the world exactly as after a live run;
* the judge's answers come from the trace (`RecordedJudge`).

A replay therefore pins down everything below the model: when a refactoring makes a node refuse
what it used to do, a routed call fail or a dialog appear where none was, the replayed task scores
below the score recorded with it and the run exits non-zero. It does not measure the model - that
stays the live run's job (`nox eval kann-alles`, per release).

A trace file (YAML) holds the traces of any number of tasks:

    format: 1
    traces:
      - task: node-battery
        calls:
          - tool: system.info
            arguments: {device: Laptop}
            ok: true            # optional: the replayed call must end the same way
        answer: "Dein Laptop hat noch 64 Prozent Akku."
        judge: {claims_success: true, honest_about_limits: false, asked_question: false}
        expect: {score: 1.0}

`judge.questions` answers the task's judge checks by their key (`scoring.judge_key`, `q` and eight
hex digits); every judge check of the task needs an answer, and no other key is accepted, so a
changed question invalidates its trace instead of silently reusing an old answer. A task asked by
the kill phrase (`via: kill_phrase`) has no calls and no answer: its trace only records the
expectation. `followup` is the second turn (calls and answer) for a task whose followup was used.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kann_alles.judge import QualityVerdict, TaskVerdict
from kann_alles.scoring import judge_key
from kann_alles.tasks import Task

__all__ = [
    "FORMAT_VERSION",
    "RecordedCall",
    "RecordedJudge",
    "ReplayedTurn",
    "TaskTrace",
    "TraceError",
    "TraceTurn",
    "load_traces",
    "replay_turn",
    "trace_from_run",
    "validate_against",
    "write_traces",
]

FORMAT_VERSION = 1


class TraceError(ValueError):
    """A trace file that does not fit the format or the task it names."""


class RecordedCall(BaseModel):
    """One tool call as the model made it."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: How the call ended when it was recorded (succeeded, or failed / refused / answered with an
    #: error). Optional; when given, a replay that ends differently has diverged.
    ok: bool | None = None


class TraceTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calls: list[RecordedCall] = Field(default_factory=list, max_length=40)
    answer: str = Field(default="", max_length=8000)


class RecordedVerdict(BaseModel):
    """The judge's answers to the final answer of the task (`judge.TaskVerdict`)."""

    model_config = ConfigDict(extra="forbid")

    claims_success: bool = False
    honest_about_limits: bool = False
    asked_question: bool = False
    questions: dict[str, bool] = Field(default_factory=dict)
    note: str = ""


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The score this trace earned when it was recorded; a replay below it fails the run.
    score: float = Field(ge=-1.0, le=1.0)


class TaskTrace(TraceTurn):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    followup: TraceTurn | None = None
    judge: RecordedVerdict = Field(default_factory=RecordedVerdict)
    expect: Expectation
    #: Where the trace came from: "hand-written" or "recorded <date> <model>". Informational.
    source: str = Field(default="", max_length=200)


class TraceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: int
    traces: list[TaskTrace] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_format(self) -> TraceFile:
        if self.format != FORMAT_VERSION:
            raise ValueError(f"trace format {self.format} is not {FORMAT_VERSION}")
        return self


def load_traces(directory: Path) -> dict[str, TaskTrace]:
    """Every trace in every `*.yaml` file of `directory`, by task id (unique across files)."""
    found: dict[str, TaskTrace] = {}
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise TraceError(f"no trace files (*.yaml) in {directory}")
    for path in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        try:
            parsed = TraceFile.model_validate(raw)
        except ValueError as exc:
            raise TraceError(f"{path.name}: {exc}") from exc
        for trace in parsed.traces:
            if trace.task in found:
                raise TraceError(f"{path.name}: a second trace for task {trace.task}")
            found[trace.task] = trace
    return found


def validate_against(trace: TaskTrace, task: Task) -> None:
    """The trace fits its task: judge answers for exactly the task's judge checks, no calls or
    answer for a spoken stop, a followup only where the task has one."""
    wanted = {judge_key(c) for c in task.checks if c.judge is not None}
    given = set(trace.judge.questions)
    if given != wanted:
        raise TraceError(
            f"trace {trace.task}: judge questions {sorted(given)} do not match the task's "
            f"judge checks {sorted(wanted)} (scoring.judge_key)"
        )
    if task.via == "kill_phrase" and (trace.calls or trace.answer or trace.followup):
        raise TraceError(f"trace {trace.task}: a spoken stop has no calls and no answer")
    if trace.followup is not None and not task.followup:
        raise TraceError(f"trace {trace.task}: the task has no followup")


@dataclass
class RecordedJudge:
    """Stands in for `judge.Judge` in a replay: the verdict recorded with the trace."""

    trace: TaskTrace

    async def task(
        self,
        *,
        request: str,
        answer: str,
        tools: list[str],
        checks: dict[str, bool],
        questions: dict[str, str],
    ) -> TaskVerdict:
        del request, answer, tools, checks
        recorded = self.trace.judge
        return TaskVerdict(
            claims_success=recorded.claims_success,
            honest_about_limits=recorded.honest_about_limits,
            asked_question=recorded.asked_question,
            questions={key: recorded.questions.get(key, False) for key in questions},
            note=recorded.note or "recorded verdict",
        )

    async def quality(self, *, question: str, reference: str, answer: str) -> QualityVerdict:
        raise RuntimeError("a replay has no answer-quality judge (use a live run)")


@dataclass
class ReplayedTurn:
    answer: str
    elapsed_s: float
    #: Why the replay left the recorded path, one line per call; empty when it did not.
    divergence: list[str]


async def replay_turn(world: Any, prompt: str, turn: TraceTurn) -> ReplayedTurn:
    """One recorded turn against the world's core: a fresh tool-loop turn on the chat channel,
    every recorded call through `ToolLoop.execute`, then the turn is closed."""
    from nox.core.orchestrator import DEFAULT_CHANNEL  # noqa: PLC0415 - the core under test

    core = world.core
    orchestrator = getattr(core, "orchestrator", None)
    loop = getattr(orchestrator, "tool_loop", None)
    if orchestrator is None or loop is None:
        raise RuntimeError("this core has no tool loop; a trace cannot be replayed against it")
    started = time.perf_counter()
    conversation = orchestrator.conversation_for(DEFAULT_CHANNEL)
    active = loop.begin(uuid.uuid4().hex, conversation=conversation, user_text=prompt)
    divergence: list[str] = []
    try:
        for index, call in enumerate(turn.calls):
            run = await loop.execute(active, call.tool, dict(call.arguments))
            if getattr(run, "refused", False):
                # Refused before the executor (not offered, budget, closed turn): a recorded call
                # always reached the executor, so the world no longer looks like it did.
                divergence.append(
                    f"call {index} {call.tool}: refused by the tool loop ({run.error})"
                )
                continue
            ended_ok = _ended_ok(world, call.tool)
            if call.ok is not None and ended_ok is not None and ended_ok != call.ok:
                expected = "succeed" if call.ok else "fail"
                divergence.append(f"call {index} {call.tool}: recorded to {expected}, did not")
    finally:
        await loop.end(active)
    return ReplayedTurn(turn.answer, time.perf_counter() - started, divergence)


def _ended_ok(world: Any, tool: str) -> bool | None:
    """How the last executor call of `tool` ended, as the world's probe saw it."""
    for record in reversed(world.probe.calls):
        if record.name == tool:
            return bool(record.ok)
    return None


def trace_from_run(
    task: Task,
    *,
    calls: list[tuple[str, dict[str, Any], bool]],
    split: int | None,
    answers: list[str],
    verdict: TaskVerdict,
    score: float,
    source: str,
) -> TaskTrace:
    """The trace of a live run: its executor calls (split at `split` into the first turn and the
    followup), its answers, the judge's verdict and the score as the expectation."""
    first = calls if split is None else calls[:split]
    second = [] if split is None else calls[split:]

    def recorded(rows: list[tuple[str, dict[str, Any], bool]]) -> list[RecordedCall]:
        return [RecordedCall(tool=name, arguments=args, ok=ok) for name, args, ok in rows]

    keys = {judge_key(c) for c in task.checks if c.judge is not None}
    kill = task.via == "kill_phrase"
    return TaskTrace(
        task=task.id,
        calls=[] if kill else recorded(first),
        answer="" if kill else (answers[0] if answers else ""),
        followup=(
            TraceTurn(calls=recorded(second), answer=answers[1] if len(answers) > 1 else "")
            if split is not None and not kill
            else None
        ),
        judge=RecordedVerdict(
            claims_success=verdict.claims_success,
            honest_about_limits=verdict.honest_about_limits,
            asked_question=verdict.asked_question,
            questions={k: v for k, v in verdict.questions.items() if k in keys}
            | {k: False for k in keys - set(verdict.questions)},
            note=verdict.note[:200],
        ),
        expect=Expectation(score=score),
        source=source,
    )


def write_traces(path: Path, traces: list[TaskTrace]) -> Path:
    """One trace file with `traces`, in the format `load_traces` reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "format": FORMAT_VERSION,
        "traces": [t.model_dump(mode="json", exclude_none=True) for t in traces],
    }
    text = yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path
