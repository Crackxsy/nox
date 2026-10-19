"""Command line of the benchmark (also reached through `nox eval kann-alles`)."""

from __future__ import annotations

import argparse
import asyncio
import platform
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kann_alles.env import World
from kann_alles.judge import Judge
from kann_alles.runner import (
    as_dicts,
    run_latency,
    run_quality,
    run_task,
    summarize_latency,
    summarize_tasks,
    write_reports,
)
from kann_alles.tasks import load_quality, load_tasks

HERE = Path(__file__).resolve().parent
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_CORE = 2


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
    return parser.parse_args(argv)


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


async def _run(ns: argparse.Namespace, nox_src: Path) -> int:
    parts = {p.strip() for p in ns.parts.split(",") if p.strip()}
    tasks = load_tasks(HERE / "tasks")
    if ns.only:
        wanted = {w.strip() for w in ns.only.split(",")}
        tasks = [t for t in tasks if t.id in wanted or t.category.value in wanted]
    quality = load_quality(HERE / "quality.yaml")
    judge = Judge(model=ns.judge_model)
    payload: dict[str, Any] = {
        "label": ns.label,
        "meta": {
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "nox_rev": _git_rev(nox_src),
            "judge": ns.judge_model,
            "privacy_mode": ns.privacy_mode,
        },
    }
    with tempfile.TemporaryDirectory(prefix="nox-eval-") as tmp:
        world = World(Path(tmp), privacy_mode=ns.privacy_mode)
        try:
            await world.start()
        except Exception as exc:  # noqa: BLE001 - reported with an exit code
            print(f"core did not boot: {type(exc).__name__}: {exc}")
            await world.stop()
            return EXIT_NO_CORE
        try:
            if "tasks" in parts:
                results = []
                for task in tasks:
                    row = await run_task(world, task, judge)
                    print(f"{row.id:<40} {row.score:>5}  {row.reason}")
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
    print(f"wrote {json_path} and {md_path}")
    return EXIT_OK


def run(argv: list[str], *, nox_src: Path) -> int:
    ns = _parse(argv)
    return asyncio.run(_run(ns, nox_src))
