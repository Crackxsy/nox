"""Which local model can actually drive Nox's tools?

Speed is the obvious question about a local model and the less important one. Nox reaches its tools
through a text protocol - the model answers with one `NOX_TOOL_CALL {...}` line and nothing else -
so a model that is fast and cannot hold that shape can talk and do nothing. This asks every model
Ollama has the real question, using the real offer text and the real parser from `nox.ai.tooluse`,
and reports both: does it hit the protocol, and how fast.

    python scripts/probe_models.py
    python scripts/probe_models.py --models qwen3:4b-instruct,mistral:latest

No core is booted and no tool is executed: each answer is parsed, never run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from nox.ai.tooluse import OfferedTool, ToolCall, parse_directive, render_offer

OLLAMA = "http://127.0.0.1:11434"

#: A slice of what Nox really offers, kept small so the probe measures the protocol rather than the
#: model's ability to read forty tool descriptions.
TOOLS = [
    OfferedTool(
        name="home.light",
        description="Switch or dim a light.",
        schema={
            "properties": {"entity_ids": {"type": "array"}, "on": {"type": "boolean"}},
            "required": ["entity_ids"],
        },
    ),
    OfferedTool(
        name="file.list",
        description="What is in one folder.",
        schema={"properties": {"path": {"type": "string"}}, "required": ["path"]},
    ),
    OfferedTool(name="desktop.windows", description="The open windows.", schema={"properties": {}}),
    OfferedTool(
        name="capabilities.list",
        description="What you can do right now.",
        schema={"properties": {}},
    ),
]

#: German, because that is what the user writes, against English tool names - which is the case
#: this protocol actually has to survive.
CASES: list[tuple[str, str | None]] = [
    ("Was liegt in C:/Users/test/Downloads?", "file.list"),
    ("Welche Fenster sind gerade offen?", "desktop.windows"),
    ("Was kannst du eigentlich gerade alles?", "capabilities.list"),
    ("Erzähl mir einen kurzen Witz.", None),  # must NOT reach for a tool
]


async def ask(client: httpx.AsyncClient, model: str, prompt: str, offer: str) -> tuple[str, float]:
    started = time.perf_counter()
    reply = await client.post(
        f"{OLLAMA}/api/chat",
        json={
            "model": model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": f"You are Nox.\n\n{offer}"},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=180.0,
    )
    reply.raise_for_status()
    body = reply.json()
    return str(body.get("message", {}).get("content", "")), time.perf_counter() - started


async def probe(model: str, offer: str, names: list[str]) -> dict[str, Any]:
    hits, total, seconds = 0, 0, 0.0
    notes: list[str] = []
    async with httpx.AsyncClient() as client:
        for prompt, wanted in CASES:
            total += 1
            try:
                answer, took = await ask(client, model, prompt, offer)
            except Exception as exc:  # noqa: BLE001 - a model that will not answer is a result
                notes.append(f"{prompt[:24]}: {type(exc).__name__}")
                continue
            seconds += took
            parsed = parse_directive(answer, names)
            if wanted is None:
                # The harder half: not reaching for a tool when none is needed.
                if parsed is None:
                    hits += 1
                else:
                    got = parsed.name if isinstance(parsed, ToolCall) else "a broken directive"
                    notes.append(f"chatted -> {got}")
            elif isinstance(parsed, ToolCall) and parsed.name == wanted:
                hits += 1
            elif isinstance(parsed, ToolCall):
                notes.append(f"{wanted} -> {parsed.name}")
            elif isinstance(parsed, str):
                notes.append(f"{wanted} -> malformed")
            else:
                notes.append(f"{wanted} -> prose")
    return {
        "model": model,
        "hits": hits,
        "of": total,
        "seconds_per_answer": round(seconds / max(1, total), 1),
        "notes": notes,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="", help="comma-separated; default is every local one")
    parser.add_argument("--json", default="", help="also write the results here")
    args = parser.parse_args()

    async with httpx.AsyncClient() as client:
        listing = await client.get(f"{OLLAMA}/api/tags", timeout=30.0)
    available = [m["name"] for m in listing.json().get("models", [])]
    wanted = [m.strip() for m in args.models.split(",") if m.strip()] or available
    # Embedding models have no chat endpoint worth asking.
    wanted = [m for m in wanted if "embed" not in m]

    offer = render_offer(TOOLS)
    names = [tool.name for tool in TOOLS]
    results = []
    for model in wanted:
        print(f"  {model} ...", flush=True)
        results.append(await probe(model, offer, names))

    results.sort(key=lambda r: (-r["hits"], r["seconds_per_answer"]))
    print(f"\n{'model':26} {'protocol':>10} {'s/answer':>9}  notes")
    for row in results:
        note = "; ".join(row["notes"][:2]) or "-"
        score = f"{row['hits']}/{row['of']}"
        print(f"{row['model']:26} {score:>10} {row['seconds_per_answer']:>9}  {note}")
    if args.json:
        # Off the loop: writing is blocking, and this is the one place it happens.
        await asyncio.to_thread(
            Path(args.json).write_text,
            json.dumps(results, indent=2, ensure_ascii=False),
            "utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
