"""The recorded-trace replay of the capability benchmark (`kann_alles.traces`): the committed
traces fit their tasks, a replay drives the core's tool loop and nothing else, a recorded verdict
stands in for the judge, and the run fails when a task scores below its trace."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kann_alles.cli import EXIT_BELOW_EXPECTATION, EXIT_OK, record, replay_set, replay_verdict
from kann_alles.judge import TaskVerdict
from kann_alles.runner import TaskResult
from kann_alles.scoring import judge_key
from kann_alles.tasks import Task, load_tasks
from kann_alles.traces import (
    FORMAT_VERSION,
    RecordedJudge,
    TaskTrace,
    TraceError,
    TraceTurn,
    load_traces,
    replay_turn,
    trace_from_run,
    validate_against,
    write_traces,
)

EVALS = Path(__file__).resolve().parents[3] / "evals" / "kann_alles"
TASKS = EVALS / "tasks"
TRACES = EVALS / "traces"


def _tasks() -> dict[str, Task]:
    return {t.id: t for t in load_tasks(TASKS)}


def _trace(task: str, **extra: Any) -> TaskTrace:
    return TaskTrace.model_validate({"task": task, "expect": {"score": 1.0}, **extra})


def test_every_committed_trace_fits_its_task_and_covers_the_node_tasks() -> None:
    tasks = _tasks()
    traces = load_traces(TRACES)
    chosen, _ = replay_set(list(tasks.values()), TRACES, "")
    assert {t.id for t in chosen} == set(traces)
    node_tasks = {
        t.id for t in tasks.values() if t.id.startswith("node-") or t.id.startswith("pi-")
    }
    assert node_tasks <= set(traces), node_tasks - set(traces)
    for trace in traces.values():
        assert trace.source  # hand-written or recorded: always said


def test_a_trace_must_answer_exactly_the_tasks_judge_questions() -> None:
    task = _tasks()["node-install"]
    (question,) = [judge_key(c) for c in task.checks if c.judge is not None]
    validate_against(_trace("node-install", judge={"questions": {question: True}}), task)
    with pytest.raises(TraceError, match="judge questions"):
        validate_against(_trace("node-install"), task)  # a changed question: no old answer
    with pytest.raises(TraceError, match="judge questions"):
        validate_against(
            _trace("node-install", judge={"questions": {question: True, "q00000000": True}}), task
        )


def test_a_spoken_stop_has_no_calls_and_a_followup_needs_one_in_the_task() -> None:
    tasks = _tasks()
    with pytest.raises(TraceError, match="spoken stop"):
        validate_against(_trace("node-kill", calls=[{"tool": "system.info"}]), tasks["node-kill"])
    with pytest.raises(TraceError, match="no followup"):
        validate_against(
            _trace("node-battery", followup={"answer": "noch einmal"}), tasks["node-battery"]
        )


def test_traces_for_unknown_tasks_or_unknown_formats_are_refused(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        "format: 1\ntraces:\n  - task: no-such-task\n    expect: {score: 1.0}\n", "utf-8"
    )
    with pytest.raises(TraceError, match="unknown tasks"):
        replay_set(list(_tasks().values()), tmp_path, "")
    (tmp_path / "a.yaml").write_text("format: 99\ntraces: []\n", "utf-8")
    with pytest.raises(TraceError, match="format 99"):
        load_traces(tmp_path)
    (tmp_path / "a.yaml").write_text(
        "format: 1\ntraces:\n  - task: node-battery\n    expect: {score: 1.0}\n"
        "    calls: [{tool: system.info, argumentz: {}}]\n",
        "utf-8",
    )
    with pytest.raises(TraceError):
        load_traces(tmp_path)  # a misspelt field fails the load, not the run


async def test_the_recorded_verdict_stands_in_for_the_judge() -> None:
    trace = _trace(
        "node-offline",
        judge={"honest_about_limits": True, "questions": {"qda7c750d": True}},
    )
    verdict = await RecordedJudge(trace).task(
        request="",
        answer="",
        tools=[],
        checks={},
        questions={"qda7c750d": "...", "q11111111": "..."},
    )
    assert verdict.honest_about_limits and not verdict.claims_success
    assert verdict.questions == {"qda7c750d": True, "q11111111": False}
    with pytest.raises(RuntimeError, match="no answer-quality judge"):
        await RecordedJudge(trace).quality(question="", reference="", answer="")


@dataclass
class _Run:
    ok: bool
    refused: bool = False
    error: str = ""


@dataclass
class _Record:
    name: str
    ok: bool


@dataclass
class _Loop:
    """The core's tool loop as a replay sees it; records what it was asked to do."""

    outcome: dict[str, _Run]
    probe: Any
    begun: list[tuple[str, str]] = field(default_factory=list)
    executed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    ended: int = 0

    def begin(self, turn_id: str, *, conversation: str = "", user_text: str = "") -> object:
        self.begun.append((conversation, user_text))
        return object()

    async def execute(self, _turn: object, name: str, arguments: dict[str, Any]) -> _Run:
        self.executed.append((name, arguments))
        run = self.outcome[name]
        if not run.refused:
            self.probe.calls.append(_Record(name, run.ok))
        return run

    async def end(self, _turn: object) -> None:
        self.ended += 1


@dataclass
class _World:
    loop: _Loop | None = None
    probe: Any = None
    core: Any = None


def _world(outcome: dict[str, _Run]) -> _World:
    @dataclass
    class Probe:
        calls: list[_Record] = field(default_factory=list)

    @dataclass
    class Orchestrator:
        tool_loop: Any

        def conversation_for(self, channel: str) -> str:
            return f"{channel}#test"

    @dataclass
    class Core:
        orchestrator: Any

    probe = Probe()
    loop = _Loop(outcome, probe)
    return _World(loop=loop, probe=probe, core=Core(Orchestrator(loop)))


async def test_a_replay_runs_every_recorded_call_through_the_tool_loop_in_order() -> None:
    world = _world({"system.info": _Run(ok=True), "notify.show": _Run(ok=False)})
    turn = TraceTurn.model_validate(
        {
            "calls": [
                {"tool": "system.info", "arguments": {"device": "Laptop"}, "ok": True},
                {"tool": "notify.show", "arguments": {"device": "Laptop", "text": "x"}},
            ],
            "answer": "Fertig.",
        }
    )
    replayed = await replay_turn(world, "Wie viel Akku?", turn)
    assert world.loop is not None
    assert world.loop.begun == [("chat#test", "Wie viel Akku?")]  # the user's words, as live
    assert world.loop.executed == [
        ("system.info", {"device": "Laptop"}),
        ("notify.show", {"device": "Laptop", "text": "x"}),
    ]
    assert world.loop.ended == 1
    assert replayed.answer == "Fertig." and replayed.divergence == []


async def test_a_replay_that_ends_differently_or_is_refused_has_diverged() -> None:
    world = _world(
        {
            "system.info": _Run(ok=False, error="device_offline"),
            "screen.capture": _Run(ok=False, refused=True, error="tool.not_offered"),
        }
    )
    turn = TraceTurn.model_validate(
        {"calls": [{"tool": "system.info", "ok": True}, {"tool": "screen.capture", "ok": False}]}
    )
    replayed = await replay_turn(world, "", turn)
    assert len(replayed.divergence) == 2
    assert "recorded to succeed" in replayed.divergence[0]
    assert "refused by the tool loop (tool.not_offered)" in replayed.divergence[1]


async def test_a_core_without_a_tool_loop_cannot_be_replayed() -> None:
    world = _world({})
    world.core.orchestrator.tool_loop = None
    with pytest.raises(RuntimeError, match="no tool loop"):
        await replay_turn(world, "", TraceTurn())


def _row(task: str, score: float, expected: float | None, **extra: Any) -> TaskResult:
    base: dict[str, Any] = {
        "id": task,
        "category": "nodes",
        "kind": "solve",
        "title": task,
        "score": score,
        "solved": score > 0,
        "violation": score < 0,
        "reason": "test",
        "answer": "",
        "tools": [],
        "confirmations": 0,
        "elapsed_s": 0.1,
        "checks": {},
        "judge": {},
        "needs": "",
        "expected": expected,
    }
    return TaskResult(**{**base, **extra})


def test_the_run_fails_below_the_recorded_score_on_divergence_and_below_min_score(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert replay_verdict([_row("a", 1.0, 1.0), _row("b", 1.0, 0.5)], None) == EXIT_OK
    assert replay_verdict([_row("a", 0.5, 1.0)], None) == EXIT_BELOW_EXPECTATION
    assert "scored 0.5, recorded 1.0" in capsys.readouterr().out
    diverged = _row("a", 1.0, 1.0, divergence=["call 0 x: recorded to succeed, did not"])
    assert replay_verdict([diverged], None) == EXIT_BELOW_EXPECTATION
    assert replay_verdict([_row("a", 1.0, 1.0, error="Boom")], None) == EXIT_BELOW_EXPECTATION
    assert replay_verdict([_row("a", 0.5, 0.5)], 0.75) == EXIT_BELOW_EXPECTATION
    assert "below --min-score" in capsys.readouterr().out


def test_a_live_run_is_recorded_as_a_trace_that_loads_and_fits(tmp_path: Path) -> None:
    task = _tasks()["node-offline"]
    (question,) = [judge_key(c) for c in task.checks if c.judge is not None]
    row = _row(
        "node-offline",
        0.5,
        None,
        answer="Der Laptop ist offline.",
        judge=TaskVerdict(honest_about_limits=True, questions={question: True}).model_dump(),
        calls=[{"tool": "browser.open", "arguments": {"device": "Laptop"}, "ok": False}],
    )
    path = record(tmp_path, "live", [task], [row], {"date": "2026-10-23", "nox_rev": "abc1234"})
    trace = load_traces(tmp_path)["node-offline"]
    assert path.name == "live.yaml"
    validate_against(trace, task)
    assert [(c.tool, c.arguments, c.ok) for c in trace.calls] == [
        ("browser.open", {"device": "Laptop"}, False)
    ]
    assert trace.answer == "Der Laptop ist offline." and trace.expect.score == 0.5
    assert trace.source.startswith("recorded 2026-10-23")


def test_a_recorded_followup_is_split_at_the_second_turn(tmp_path: Path) -> None:
    task = _tasks()["node-battery"]
    trace = trace_from_run(
        task,
        calls=[("system.info", {}, False), ("system.info", {"device": "Laptop"}, True)],
        split=1,
        answers=["Welches Gerät?", "64 Prozent."],
        verdict=TaskVerdict(claims_success=True),
        score=0.5,
        source="recorded",
    )
    assert [c.ok for c in trace.calls] == [False]
    assert trace.followup is not None and trace.followup.answer == "64 Prozent."
    written = write_traces(tmp_path / "t.yaml", [trace])
    assert written.read_text("utf-8").startswith(f"format: {FORMAT_VERSION}\n")
    assert load_traces(tmp_path)["node-battery"].followup is not None
