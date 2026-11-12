"""Command line of the benchmark (also reached through `nox eval kann-alles`).

`--replay <dir>` runs the tasks that have a recorded trace there (`kann_alles.traces`) without a
model and without the judge - deterministic, for CI - and exits with `EXIT_BELOW_EXPECTATION`
when a task scores below the score its trace was recorded with, a replay left the recorded path,
a trace does not fit its task, or the quote is below `--min-score`. `--record <dir>` writes the
traces of a live run, so a run with a real model can become the next replay.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kann_alles.env import World
from kann_alles.judge import Judge, TaskVerdict
from kann_alles.runner import (
    TaskResult,
    as_dicts,
    run_latency,
    run_quality,
    run_task,
    summarize_latency,
    summarize_tasks,
    write_reports,
)
from kann_alles.tasks import Task, load_quality, load_tasks
from kann_alles.traces import (
    TaskTrace,
    TraceError,
    load_traces,
    trace_from_run,
    validate_against,
    write_traces,
)

HERE = Path(__file__).resolve().parent
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_CORE = 2
#: A replay scored below what its traces were recorded with, diverged, or a trace is invalid.
EXIT_BELOW_EXPECTATION = 3
#: How the answers of a task's turns are joined in `TaskResult.answer`.
ANSWER_SEPARATOR = "\n---\n"


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="kann-alles", description=__doc__)
    parser.add_argument("--label", default="run")
    parser.add_argument("--out", type=Path, default=Path("eval-results"))
    parser.add_argument(
        "--parts",
        default="tasks,quality,latency",
        help="comma-separated subset of: tasks, quality, latency",
    )
    parser.add_argument("--only", default="", help="comma-separated task ids or categories")
    parser.add_argument("--latency-runs", type=int, default=2)
    parser.add_argument("--judge-model", default="opus")
    parser.add_argument("--privacy-mode", default="balanced")
    parser.add_argument("--log-level", default="WARNING", help="core log level (DEBUG for traces)")
    parser.add_argument(
        "--real",
        action="store_true",
        help="run against the real machine (tools may change it). HIGH-risk tools (shell, "
        "package) are still refused unless --isolated-env confirms a VM/container with no data.",
    )
    parser.add_argument(
        "--isolated-env",
        action="store_true",
        help="assert this is an isolated environment (VM, container, Windows Sandbox); only then "
        "may --real approve HIGH-risk dialogs (shell, package).",
    )
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="replay the recorded traces in this folder: no model, no judge (deterministic)",
    )
    parser.add_argument(
        "--record",
        type=Path,
        default=None,
        help="write a trace of every task of this live run into this folder",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        help="with --replay: fail when the quote of the replayed tasks is below this",
    )
    ns = parser.parse_args(argv)
    if ns.replay is not None and ns.record is not None:
        parser.error("--replay and --record exclude each other")
    if ns.replay is not None and _parts(ns.parts) != {"tasks"}:
        parser.error("--replay replays tasks only: pass --parts tasks")
    if ns.min_score is not None and ns.replay is None:
        parser.error("--min-score belongs to --replay")
    if ns.replay is not None and ns.real:
        parser.error("--replay runs in the simulated world only")
    return ns


def _parts(text: str) -> set[str]:
    return {p.strip() for p in text.split(",") if p.strip()}


def _git_rev(src: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=0x08000000 if platform.system() == "Windows" else 0,
        )
    except OSError:
        return "unknown"
    return out.stdout.strip() or "unknown"


def run_order(tasks: list[Task]) -> list[Task]:
    """The tasks in file order, except that a spoken stop runs after everything else: it stops
    the whole core, so a stop (and the resume after it) can never colour another task."""
    return sorted(tasks, key=lambda t: t.via == "kill_phrase")


def select(tasks: list[Task], only: str) -> list[Task]:
    """`--only`: task ids or categories, comma-separated; empty means every task."""
    wanted = {w.strip() for w in only.split(",") if w.strip()}
    if not wanted:
        return tasks
    return [t for t in tasks if t.id in wanted or t.category.value in wanted]


def replay_set(
    tasks: list[Task], folder: Path, only: str
) -> tuple[list[Task], dict[str, TaskTrace]]:
    """The tasks that have a trace (narrowed by `--only`), every trace checked against its task.
    A trace for a task that does not exist is an error: a renamed task must not lose its replay
    silently."""
    traces = load_traces(folder)
    by_id = {t.id: t for t in tasks}
    unknown = sorted(set(traces) - set(by_id))
    if unknown:
        raise TraceError(f"traces for unknown tasks: {unknown}")
    for trace in traces.values():
        validate_against(trace, by_id[trace.task])
    chosen = [t for t in select(tasks, only) if t.id in traces]
    if not chosen:
        raise TraceError("no task with a trace matches --only")
    return chosen, traces


async def _run(ns: argparse.Namespace, nox_src: Path) -> int:
    parts = _parts(ns.parts)
    all_tasks = load_tasks(HERE / "tasks")
    traces: dict[str, TaskTrace] = {}
    if ns.replay is not None:
        try:
            tasks, traces = replay_set(all_tasks, ns.replay, ns.only)
        except TraceError as exc:
            print(f"invalid traces: {exc}")
            return EXIT_BELOW_EXPECTATION
    else:
        tasks = select(all_tasks, ns.only)
    tasks = run_order(tasks)
    quality = load_quality(HERE / "quality.yaml")
    judge = Judge(model=ns.judge_model)
    replaying = ns.replay is not None
    payload: dict[str, Any] = {
        "label": ns.label,
        "meta": {
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "nox_rev": _git_rev(nox_src),
            "mode": "replay" if replaying else "live",
            "judge": "recorded verdicts" if replaying else ns.judge_model,
            "privacy_mode": ns.privacy_mode,
        },
    }
    isolated = bool(getattr(ns, "real", False) and getattr(ns, "isolated_env", False))
    results: list[TaskResult] = []
    with tempfile.TemporaryDirectory(prefix="nox-eval-") as tmp:
        world = World(
            Path(tmp),
            privacy_mode=ns.privacy_mode,
            log_level=ns.log_level,
            isolated=isolated,
        )
        try:
            await world.start()
        except Exception as exc:  # noqa: BLE001 - reported with an exit code
            print(f"core did not boot: {type(exc).__name__}: {exc}")
            await world.stop()
            return EXIT_NO_CORE
        try:
            if "tasks" in parts:
                for task in tasks:
                    row = await run_task(world, task, judge, traces.get(task.id))
                    recorded = "" if row.expected is None else f"  (recorded {row.expected})"
                    print(f"{row.id:<40} {row.score:>5}  {row.reason}{recorded}")
                    for line in row.divergence:
                        print(f"{'':<40} diverged: {line}")
                    results.append(row)
                payload["tasks"] = {
                    "summary": summarize_tasks(results),
                    "results": as_dicts(results),
                }
            if "quality" in parts:
                rows = await run_quality(world, quality, judge)
                scores = [float(r["score"]) for r in rows]
                payload["quality"] = {
                    "mean": round(sum(scores) / len(scores), 3) if scores else 0.0,
                    "rows": rows,
                }
                print(f"quality mean {payload['quality']['mean']}")
            if "latency" in parts:
                samples = await run_latency(world, ns.latency_runs)
                payload["latency"] = {
                    "summary": summarize_latency(samples),
                    "samples": as_dicts(samples),
                }
        finally:
            await world.stop()
    json_path, md_path = write_reports(payload, ns.out, ns.label)
    jsonl_path = write_jsonl(as_dicts(results), ns.out, ns.label)
    print(f"wrote {json_path}, {md_path} and {jsonl_path}")
    if ns.record is not None:
        record(ns.record, ns.label, tasks, results, payload["meta"])
    if replaying:
        return replay_verdict(results, ns.min_score)
    return EXIT_OK


def write_jsonl(rows: list[dict[str, Any]], out_dir: Path, label: str) -> Path:
    """One JSON object per task: CI's result of a replay, and the per-release record a live run
    leaves."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{label}.jsonl"
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def replay_verdict(results: list[TaskResult], min_score: float | None) -> int:
    """`EXIT_OK` when every replayed task reached its recorded score without leaving the recorded
    path (and the quote reached `min_score`), else `EXIT_BELOW_EXPECTATION`, each reason said."""
    failed = False
    for row in results:
        if row.expected is not None and row.score < row.expected:
            print(f"FAIL {row.id}: scored {row.score}, recorded {row.expected} ({row.reason})")
            failed = True
        if row.divergence:
            print(f"FAIL {row.id}: the replay left the recorded path")
            failed = True
        if row.error:
            print(f"FAIL {row.id}: {row.error}")
            failed = True
    quote = sum(r.score for r in results) / len(results) if results else 0.0
    if min_score is not None and quote < min_score:
        print(f"FAIL quote {quote:.3f} is below --min-score {min_score}")
        failed = True
    print(f"replayed {len(results)} tasks, quote {quote:.3f}: {'FAILED' if failed else 'ok'}")
    return EXIT_BELOW_EXPECTATION if failed else EXIT_OK


def record(
    folder: Path, label: str, tasks: list[Task], results: list[TaskResult], meta: dict[str, Any]
) -> Path:
    """The traces of this live run, in `<folder>/<label>.yaml` (a crashed task is left out)."""
    by_id = {t.id: t for t in tasks}
    source = f"recorded {meta.get('date', '')}, nox {meta.get('nox_rev', '')}"
    traces = [
        trace_from_run(
            by_id[row.id],
            calls=[(str(c["tool"]), dict(c["arguments"]), bool(c["ok"])) for c in row.calls],
            split=row.followup_at,
            answers=row.answer.split(ANSWER_SEPARATOR) if row.answer else [],
            verdict=TaskVerdict.model_validate(row.judge),
            score=row.score,
            source=source,
        )
        for row in results
        if not row.error
    ]
    path = write_traces(folder / f"{label}.yaml", traces)
    print(f"wrote {len(traces)} traces to {path}")
    return path


def run(argv: list[str], *, nox_src: Path) -> int:
    ns = _parse(argv)
    return asyncio.run(_run(ns, nox_src))
