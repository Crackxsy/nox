"""Runs the benchmark: tasks, the answer-quality set and the latency probe, against one booted core.

Results are written as JSON (everything, for comparison runs) and Markdown (a readable summary).
The runner never decides what a good answer is - the checks and the judge do - it only drives the
core the way a user would and records what happened.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kann_alles.env import Probe, World
from kann_alles.judge import Judge, QualityVerdict, TaskVerdict
from kann_alles.scoring import Observation, Outcome, evaluate_check, judge_key, score
from kann_alles.tasks import QualityPrompt, Task

TURN_TIMEOUT_S = 240.0

#: Latency probe prompts. Short chat answers (the felt latency of a companion) and requests that
#: need exactly one tool. Avoid phrases the deterministic fast path answers without a model.
LATENCY_CHAT = (
    "Erzähl mir in einem Satz, warum der Himmel blau ist.",
    "Was ist ein gutes Frühstück vor einer Prüfung? Kurz bitte.",
    "Sag mir einen motivierenden Satz für den Feierabend.",
    "Was ist der Unterschied zwischen Wetter und Klima, in einem Satz?",
    "Nenn mir eine schnelle Idee fürs Abendessen.",
)
LATENCY_TOOL = (
    "Erinnere mich in 45 Minuten daran, die Wäsche aufzuhängen.",
    "Welche Erinnerungen habe ich gerade offen?",
    "Was liegt in meinem Arbeitsordner?",
    "Lies mir die Datei einkauf.txt aus meinem Arbeitsordner vor.",
    "Welche Projekte habe ich gerade?",
)


@dataclass
class TaskResult:
    id: str
    category: str
    kind: str
    title: str
    score: float
    solved: bool
    violation: bool
    reason: str
    answer: str
    tools: list[str]
    confirmations: int
    elapsed_s: float
    checks: dict[str, bool]
    judge: dict[str, Any]
    needs: str
    error: str = ""


@dataclass
class LatencySample:
    kind: str  # chat | tool
    mode: str  # text | voice
    prompt: str
    provider: str
    first_token_ms: float | None
    first_spoken_ms: float | None
    first_tool_ms: float | None
    total_ms: float
    tools: list[str] = field(default_factory=list)
    error: str = ""


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1))))
    return round(ordered[index], 1)


async def _turn(world: World, text: str, *, speak: bool) -> tuple[Any, float, float | None]:
    """One user turn. Returns (turn, wall seconds, first-token seconds from the text callback)."""
    assert world.core is not None and world.core.orchestrator is not None
    started = time.perf_counter()
    first: list[float] = []

    def on_chunk(_delta: str) -> None:
        if not first:
            first.append(time.perf_counter())

    turn = await asyncio.wait_for(
        world.core.orchestrator.handle_text(text, speak=speak, on_chunk=on_chunk),
        timeout=TURN_TIMEOUT_S,
    )
    return turn, time.perf_counter() - started, (first[0] - started) if first else None


def _tool_log(probe: Probe) -> list[str]:
    return [f"{c.name}: {'ok' if c.ok else 'error ' + c.error}" for c in probe.calls]


async def run_task(world: World, task: Task, judge: Judge) -> TaskResult:
    await world.prepare(task.env, confirm=task.confirm)
    prompt = world.render(task.prompt)
    answers: list[str] = []
    elapsed = 0.0
    used_followup = False
    error = ""
    try:
        turn, seconds, _ = await _turn(world, prompt, speak=False)
        answers.append(turn.response)
        elapsed += seconds
    except Exception as exc:  # noqa: BLE001 - a crashed turn is a scored row, not a crashed run
        error = f"{type(exc).__name__}: {exc}"[:300]
    obs, checks, verdict = await _assess(world, task, prompt, answers, elapsed, False, judge)
    if task.followup and not all(checks.values()) and verdict.asked_question and not error:
        used_followup = True
        try:
            turn, seconds, _ = await _turn(world, world.render(task.followup), speak=False)
            answers.append(turn.response)
            elapsed += seconds
        except Exception as exc:  # noqa: BLE001 - see above
            error = f"{type(exc).__name__}: {exc}"[:300]
        obs, checks, verdict = await _assess(
            world, task, prompt, answers, elapsed, used_followup, judge
        )
    outcome: Outcome = score(task, obs, checks, verdict)
    return TaskResult(
        id=task.id,
        category=task.category.value,
        kind=task.kind.value,
        title=task.title,
        score=outcome.score,
        solved=outcome.solved,
        violation=outcome.violation,
        reason=outcome.reason,
        answer="\n---\n".join(answers),
        tools=_tool_log(world.probe),
        confirmations=world.probe.confirmations,
        elapsed_s=round(elapsed, 2),
        checks=checks,
        judge=verdict.model_dump(),
        needs=task.needs,
        error=error,
    )


async def _assess(
    world: World,
    task: Task,
    prompt: str,
    answers: list[str],
    elapsed: float,
    used_followup: bool,
    judge: Judge,
) -> tuple[Observation, dict[str, bool], TaskVerdict]:
    probe = world.probe
    sql = {
        c.sql_rows[0]: len(world.sql_rows(c.sql_rows[0]))
        for c in task.checks
        if c.sql_rows is not None
    }
    obs = Observation(
        answer="\n".join(answers),
        tools_ok=tuple(c.name for c in probe.calls if c.ok),
        tools_attempted=tuple(c.name for c in probe.calls),
        confirmations=probe.confirmations,
        elapsed_s=elapsed,
        used_followup=used_followup,
        sql=sql,
    )
    empty = TaskVerdict()
    deterministic = {
        c.label: evaluate_check(c, world.sandbox, obs, empty)
        for c in task.checks
        if c.judge is None
    }
    questions = {judge_key(c): str(c.judge) for c in task.checks if c.judge is not None}
    verdict = await judge.task(
        request=prompt,
        answer=obs.answer,
        tools=_tool_log(probe),
        checks=deterministic,
        questions=questions,
    )
    checks = {c.label: evaluate_check(c, world.sandbox, obs, verdict) for c in task.checks}
    return obs, checks, verdict


async def run_quality(
    world: World, items: list[QualityPrompt], judge: Judge
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        await world.prepare(type(world).empty_env(), confirm="deny")
        try:
            turn, seconds, first = await _turn(world, item.prompt, speak=False)
            answer = turn.response
            error = ""
        except Exception as exc:  # noqa: BLE001 - reported per row
            answer, seconds, first, error = "", 0.0, None, f"{type(exc).__name__}: {exc}"[:300]
        verdict: QualityVerdict = await judge.quality(
            question=item.prompt, reference=item.reference, answer=answer
        )
        rows.append(
            {
                "id": item.id,
                "prompt": item.prompt,
                "answer": answer,
                "score": verdict.score,
                "verdict": verdict.model_dump(),
                "seconds": round(seconds, 2),
                "first_token_s": round(first, 2) if first is not None else None,
                "error": error,
            }
        )
    return rows


async def run_latency(world: World, runs: int) -> list[LatencySample]:
    samples: list[LatencySample] = []
    files = {"einkauf.txt": "Milch\nBrot\nÄpfel\n", "notizen.md": "# Notizen\n"}
    for _ in range(runs):
        for kind, prompts in (("chat", LATENCY_CHAT), ("tool", LATENCY_TOOL)):
            for mode in ("text", "voice"):
                for prompt in prompts:
                    env = type(world).empty_env()
                    env.files.update(files)
                    await world.prepare(env, confirm="allow", fresh_session=False)
                    samples.append(await _latency_sample(world, kind, mode, prompt))
    return samples


async def _latency_sample(world: World, kind: str, mode: str, prompt: str) -> LatencySample:
    probe = world.probe
    started = time.perf_counter()
    try:
        turn, seconds, first = await _turn(world, prompt, speak=mode == "voice")
    except Exception as exc:  # noqa: BLE001 - reported per sample
        return LatencySample(kind, mode, prompt, "", None, None, None, 0.0, error=str(exc)[:200])
    first_tool = probe.calls[0].started_s - started if probe.calls else None
    spoken = probe.first_say_s - started if probe.first_say_s is not None else None
    return LatencySample(
        kind=kind,
        mode=mode,
        prompt=prompt,
        provider=str(turn.provider),
        first_token_ms=round(first * 1000, 1) if first is not None else None,
        first_spoken_ms=round(spoken * 1000, 1) if spoken is not None else None,
        first_tool_ms=round(first_tool * 1000, 1) if first_tool is not None else None,
        total_ms=round(seconds * 1000, 1),
        tools=[c.name for c in probe.calls],
    )


def summarize_latency(samples: list[LatencySample]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kind in ("chat", "tool"):
        for mode in ("text", "voice"):
            rows = [s for s in samples if s.kind == kind and s.mode == mode and not s.error]
            key = f"{kind}/{mode}"
            entry: dict[str, Any] = {"n": len(rows)}
            for metric in ("first_token_ms", "first_spoken_ms", "first_tool_ms", "total_ms"):
                values = [float(v) for s in rows if (v := getattr(s, metric)) is not None]
                entry[metric] = {
                    "p50": _percentile(values, 50),
                    "p95": _percentile(values, 95),
                    "n": len(values),
                }
            out[key] = entry
    return out


def summarize_tasks(results: list[TaskResult]) -> dict[str, Any]:
    by_category: dict[str, list[float]] = {}
    for row in results:
        by_category.setdefault(row.category, []).append(row.score)
    elapsed = [r.elapsed_s for r in results if not r.error]
    return {
        "tasks": len(results),
        "quote": round(sum(r.score for r in results) / len(results), 3) if results else 0.0,
        "solved": sum(1 for r in results if r.solved),
        "violations": sum(1 for r in results if r.violation),
        "median_s": round(statistics.median(elapsed), 1) if elapsed else None,
        "calls": sum(len(r.tools) for r in results),
        "confirmations": sum(r.confirmations for r in results),
        "by_category": {k: round(sum(v), 2) for k, v in sorted(by_category.items())},
    }


def write_reports(payload: dict[str, Any], out_dir: Path, label: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{label}.json"
    md_path = out_dir / f"{label}.md"
    json_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [f"# Capability benchmark - {payload.get('label', '')}", ""]
    meta = payload.get("meta", {})
    lines += [f"- {key}: {value}" for key, value in meta.items()]
    tasks = payload.get("tasks")
    if tasks:
        summary = tasks["summary"]
        lines += [
            "",
            "## Tasks",
            "",
            f"Quote **{summary['quote']}** over {summary['tasks']} tasks - solved "
            f"{summary['solved']}, safety violations {summary['violations']}, median "
            f"{summary['median_s']} s, {summary['calls']} tool calls, "
            f"{summary['confirmations']} confirmations.",
            "",
            "| task | category | score | reason | tools | s |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in tasks["results"]:
            tools = ", ".join(t.split(":")[0] for t in row["tools"]) or "-"
            lines.append(
                f"| {row['id']} | {row['category']} | {row['score']} | {row['reason']} | "
                f"{tools} | {row['elapsed_s']} |"
            )
    quality = payload.get("quality")
    if quality:
        lines += [
            "",
            "## Answer quality",
            "",
            f"Mean score **{quality['mean']}** (0-1) over {len(quality['rows'])} prompts.",
            "",
            "| prompt | score | note |",
            "| --- | --- | --- |",
        ]
        for row in quality["rows"]:
            note = str(row["verdict"].get("note", "")).replace("|", "/")
            lines.append(f"| {row['id']} | {row['score']} | {note} |")
    latency = payload.get("latency")
    if latency:
        lines += [
            "",
            "## Latency (ms, P50 / P95)",
            "",
            "| set | n | first token | first spoken | first tool call | total |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for key, entry in latency["summary"].items():
            cells = [
                f"{entry[m]['p50']} / {entry[m]['p95']}"
                for m in ("first_token_ms", "first_spoken_ms", "first_tool_ms", "total_ms")
            ]
            lines.append(f"| {key} | {entry['n']} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def as_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [asdict(row) for row in rows]
