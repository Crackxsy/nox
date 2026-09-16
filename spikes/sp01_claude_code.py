"""SP-01: Claude Code CLI as chat provider - latency, streaming, logged-out behaviour.

Run:  .venv/Scripts/python.exe spikes/sp01_claude_code.py [--runs 5] [--models sonnet,haiku]
Writes spikes/results/sp01_results.json, prints a markdown table, and records sanitized fixtures
to tests/unit/ai/fixtures/claude_stream_sample.ndjson (+ claude_stream_logged_out.ndjson).
Costs quota: every run is a real request (about 0.005-0.03 USD equivalent each).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from nox.ai.base import AiRequest, AiRole, Message  # noqa: E402
from nox.ai.config import ClaudeCodeConfig  # noqa: E402
from nox.ai.errors import ProviderError, ProviderUnavailableError  # noqa: E402
from nox.ai.providers.claude_code import ClaudeCodeProvider  # noqa: E402

PROMPT_DE = "Sag mir in genau einem kurzen Satz, warum Pausen beim Programmieren sinnvoll sind."
SYSTEM = "Du bist Nox, ein knapper Begleiter. Antworte auf Deutsch in maximal einem Satz."
FIXTURE_DIR = REPO / "tests" / "unit" / "ai" / "fixtures"
RESULTS = REPO / "spikes" / "results" / "sp01_results.json"

_HEX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MSG = re.compile(r"msg_[A-Za-z0-9]+")
_REQ = re.compile(r"req_[A-Za-z0-9]+")


def sanitize(line: str) -> str:
    """Strip identifiers, paths and machine-specific lists from a stream-json line."""
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return ""
    if data.get("type") == "system" and data.get("subtype") == "init":
        keep = {"type", "subtype", "model", "session_id", "claude_code_version", "apiKeySource"}
        data = {k: v for k, v in data.items() if k in keep}
        data["cwd"] = "C:\\redacted"
        data["tools"] = []
    if data.get("type") == "rate_limit_event":
        data["rate_limit_info"] = {"status": "allowed", "rateLimitType": "five_hour"}
    text = json.dumps(data, ensure_ascii=True)
    text = _HEX.sub("00000000-0000-4000-8000-000000000000", text)
    text = _MSG.sub("msg_redacted", text)
    text = _REQ.sub("req_redacted", text)
    text = text.replace(os.environ.get("USERNAME", "\x00"), "user")
    return text


async def record_raw(
    provider: ClaudeCodeProvider, model: str, env: dict[str, str] | None, prompt: str
) -> tuple[list[str], int]:
    args = [*provider.executable(), *provider.build_args(SYSTEM, model=model)]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    out, _err = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=120)
    lines = [ln for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines, proc.returncode or 0


async def timed_run(provider: ClaudeCodeProvider, model: str, run: int) -> dict[str, Any]:
    request = AiRequest(
        request_id=f"sp01-{model}-{run}",
        role=AiRole.CHAT,
        messages=[Message(role="system", content=SYSTEM), Message(role="user", content=PROMPT_DE)],
        max_tokens=80,
        timeout_s=90,
        metadata={"model": model} if model else {},
    )
    started = time.perf_counter()
    first_ms: float | None = None
    deltas = 0
    text_parts: list[str] = []
    async for chunk in provider.stream(request):
        if chunk.delta:
            deltas += 1
            if first_ms is None:
                first_ms = (time.perf_counter() - started) * 1000
            text_parts.append(chunk.delta)
    total_ms = (time.perf_counter() - started) * 1000
    response = provider.last_response(request.request_id)
    return {
        "model": model or "(cli default)",
        "run": run,
        "ttft_ms": round(first_ms or -1),
        "total_ms": round(total_ms),
        "deltas": deltas,
        "chars": len("".join(text_parts)),
        "tokens_in": response.tokens_in if response else None,
        "tokens_out": response.tokens_out if response else None,
        "cost_usd": response.cost_usd if response else None,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--models", default=",sonnet,haiku", help="comma list; empty entry = CLI default model"
    )
    args = parser.parse_args()
    models = args.models.split(",")
    provider = ClaudeCodeProvider(ClaudeCodeConfig())
    results: dict[str, Any] = {"cli": "", "runs": [], "summary": {}, "health": {}, "logged_out": {}}

    t0 = time.perf_counter()
    info = await provider.health()
    results["health"] = {
        "status": info.status,
        "reason": info.reason,
        "version": provider.version,
        "ms": round((time.perf_counter() - t0) * 1000),
    }
    results["cli"] = provider.version
    print(f"health: {info.status} ({info.reason}) in {results['health']['ms']} ms")

    for model in models:
        for run in range(1, args.runs + 1):
            try:
                row = await timed_run(provider, model, run)
            except ProviderError as exc:
                row = {"model": model or "(cli default)", "run": run, "error": str(exc)}
            results["runs"].append(row)
            print(row)
        rows = [
            r
            for r in results["runs"]
            if r.get("model") == (model or "(cli default)") and "error" not in r
        ]
        if rows:
            warm = rows[1:] or rows
            results["summary"][model or "(cli default)"] = {
                "cold_ttft_ms": rows[0]["ttft_ms"],
                "cold_total_ms": rows[0]["total_ms"],
                "warm_ttft_ms_median": statistics.median(r["ttft_ms"] for r in warm),
                "warm_ttft_ms_max": max(r["ttft_ms"] for r in warm),
                "warm_total_ms_median": statistics.median(r["total_ms"] for r in warm),
                "streaming_deltas_median": statistics.median(r["deltas"] for r in warm),
                "cost_usd_median": statistics.median(r["cost_usd"] or 0 for r in warm),
            }

    # Logged-out simulation: an empty CLAUDE_CONFIG_DIR means no credentials for the CLI.
    with tempfile.TemporaryDirectory(prefix="nox-sp01-") as tmp:
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = tmp
        offline = ClaudeCodeProvider(ClaudeCodeConfig(model="haiku"), env=env)
        t0 = time.perf_counter()
        try:
            await offline.complete(
                AiRequest(
                    request_id="sp01-loggedout",
                    role=AiRole.CHAT,
                    messages=[Message(role="user", content="Sag Hallo")],
                    timeout_s=40,
                )
            )
            results["logged_out"] = {"outcome": "unexpected success"}
        except ProviderUnavailableError as exc:
            results["logged_out"] = {"outcome": "ProviderUnavailableError", "message": exc.message}
        except ProviderError as exc:
            results["logged_out"] = {"outcome": type(exc).__name__, "message": exc.message}
        results["logged_out"]["ms"] = round((time.perf_counter() - t0) * 1000)
        print("logged-out:", results["logged_out"])
        lo_lines, _ = await record_raw(offline, "haiku", env, "Sag Hallo")

    # Fixtures (sanitized).
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    raw_lines, _ = await record_raw(provider, "haiku", None, PROMPT_DE)
    (FIXTURE_DIR / "claude_stream_sample.ndjson").write_text(
        "\n".join(s for s in (sanitize(ln) for ln in raw_lines) if s) + "\n", encoding="utf-8"
    )
    (FIXTURE_DIR / "claude_stream_logged_out.ndjson").write_text(
        "\n".join(s for s in (sanitize(ln) for ln in lo_lines) if s) + "\n", encoding="utf-8"
    )

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        "\n| model | cold TTFT | cold total | warm TTFT med/max | warm total med | deltas | cost |"
    )
    print("|---|---|---|---|---|---|---|")
    for model, s in results["summary"].items():
        print(
            f"| {model} | {s['cold_ttft_ms']} ms | {s['cold_total_ms']} ms | "
            f"{s['warm_ttft_ms_median']:.0f}/{s['warm_ttft_ms_max']} ms | "
            f"{s['warm_total_ms_median']:.0f} ms | {s['streaming_deltas_median']:.0f} | "
            f"{s['cost_usd_median']:.4f} USD |"
        )
    print(f"\nresults: {RESULTS}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    asyncio.run(main())
