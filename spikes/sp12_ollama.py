"""Ollama local models - TTFT, tokens/s, GPU vs CPU-only, embedding latency.

Run:  .venv/Scripts/python.exe spikes/sp12_ollama.py [--models a,b] [--warm 3]
Writes spikes/results/sp12_results.json and prints markdown tables. Each model is unloaded
(keep_alive=0) before the cold run so load time is real. Do not run while Rocket League is running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from nox.ai.base import AiRequest, AiRole, Message  # noqa: E402
from nox.ai.config import OllamaConfig  # noqa: E402
from nox.ai.providers.ollama import OllamaProvider  # noqa: E402

BASE = "http://127.0.0.1:11434"
SYSTEM = "Du bist Nox, ein knapper Begleiter. Antworte auf Deutsch in maximal zwei Sätzen."
PROMPT_DE = "Erklär mir in einem Satz, warum Pausen beim Programmieren sinnvoll sind."
RESULTS = REPO / "spikes" / "results" / "sp12_results.json"


async def unload(client: httpx.AsyncClient, model: str) -> None:
    await client.post(f"{BASE}/api/generate", json={"model": model, "keep_alive": 0}, timeout=60)
    await asyncio.sleep(1.0)


async def loaded_models(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    data = (await client.get(f"{BASE}/api/ps", timeout=10)).json()
    return [
        {"name": m.get("name"), "size_vram": m.get("size_vram"), "size": m.get("size")}
        for m in data.get("models", [])
    ]


async def chat_run(
    client: httpx.AsyncClient, model: str, *, cpu_only: bool, label: str, max_tokens: int = 80
) -> dict[str, Any]:
    options: dict[str, Any] = {"temperature": 0.6, "num_predict": max_tokens}
    if cpu_only:
        options["num_gpu"] = 0
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": PROMPT_DE}],
        "stream": True,
        "options": options,
        "keep_alive": "5m",
        "think": False,
    }
    started = time.perf_counter()
    first: float | None = None
    text: list[str] = []
    final: dict[str, Any] = {}
    async with client.stream("POST", f"{BASE}/api/chat", json=payload, timeout=600) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.strip():
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise RuntimeError(chunk["error"])
            delta = chunk.get("message", {}).get("content", "")
            if delta:
                if first is None:
                    first = (time.perf_counter() - started) * 1000
                text.append(delta)
            if chunk.get("done"):
                final = chunk
                break
    total = (time.perf_counter() - started) * 1000
    eval_count = final.get("eval_count") or 0
    eval_ns = final.get("eval_duration") or 1
    return {
        "model": model,
        "label": label,
        "cpu_only": cpu_only,
        "ttft_ms": round(first or -1),
        "total_ms": round(total),
        "load_ms": round((final.get("load_duration") or 0) / 1e6),
        "prompt_eval_ms": round((final.get("prompt_eval_duration") or 0) / 1e6),
        "tokens_out": eval_count,
        "tok_s": round(eval_count / (eval_ns / 1e9), 1),
        "chars": len("".join(text)),
    }


async def embed_runs(client: httpx.AsyncClient, model: str, warm: int) -> dict[str, Any]:
    await unload(client, model)
    single = "Nox ist ein lokaler Begleiter."
    batch = [f"Satz Nummer {i} über Rocket League, Coding und Streaming." for i in range(10)]
    t0 = time.perf_counter()
    r = await client.post(
        f"{BASE}/api/embed", json={"model": model, "input": [single]}, timeout=120
    )
    r.raise_for_status()
    cold = (time.perf_counter() - t0) * 1000
    dim = len(r.json()["embeddings"][0])
    singles = []
    for _ in range(warm):
        t0 = time.perf_counter()
        (
            await client.post(
                f"{BASE}/api/embed", json={"model": model, "input": [single]}, timeout=120
            )
        ).raise_for_status()
        singles.append((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    (
        await client.post(f"{BASE}/api/embed", json={"model": model, "input": batch}, timeout=120)
    ).raise_for_status()
    batch_ms = (time.perf_counter() - t0) * 1000
    return {
        "model": model,
        "dim": dim,
        "cold_ms": round(cold),
        "warm_single_ms_median": round(statistics.median(singles)),
        "batch10_ms": round(batch_ms),
    }


async def provider_check(model: str) -> dict[str, Any]:
    """End-to-end through OllamaProvider (streaming + num_gpu=0 path)."""
    provider = OllamaProvider(OllamaConfig(model=model))
    info = await provider.health()
    request = AiRequest(
        request_id="sp12-provider",
        role=AiRole.CHAT,
        messages=[Message(role="system", content=SYSTEM), Message(role="user", content=PROMPT_DE)],
        max_tokens=60,
        mode="rocket_league",  # -> num_gpu=0 by gpu_allowed_modes
    )
    started = time.perf_counter()
    deltas = 0
    async for chunk in provider.stream(request):
        if chunk.delta:
            deltas += 1
    last = provider.last_response("sp12-provider")
    return {
        "health": f"{info.status}: {info.reason}",
        "deltas": deltas,
        "cpu_path_total_ms": round((time.perf_counter() - started) * 1000),
        "tokens_out": last.tokens_out if last else None,
        "payload_num_gpu": provider.build_payload(request, stream=True)["options"].get("num_gpu"),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="llama3.2:3b,gemma4:e2b,mistral")
    parser.add_argument("--warm", type=int, default=3)
    parser.add_argument("--skip-cpu", action="store_true")
    args = parser.parse_args()
    results: dict[str, Any] = {"runs": [], "summary": {}, "embedding": {}, "provider": {}}
    async with httpx.AsyncClient() as client:
        version = (await client.get(f"{BASE}/api/version", timeout=10)).json()
        results["ollama_version"] = version.get("version")
        for model in args.models.split(","):
            for cpu_only in [False, True] if not args.skip_cpu else [False]:
                await unload(client, model)
                rows = []
                try:
                    rows.append(await chat_run(client, model, cpu_only=cpu_only, label="cold"))
                    rows[-1]["loaded"] = await loaded_models(client)
                    for i in range(args.warm):
                        rows.append(
                            await chat_run(client, model, cpu_only=cpu_only, label=f"warm{i + 1}")
                        )
                except Exception as exc:  # noqa: BLE001 - report, keep measuring the rest
                    rows.append({"model": model, "cpu_only": cpu_only, "error": repr(exc)})
                for row in rows:
                    print(row)
                results["runs"].extend(rows)
                good = [r for r in rows if "error" not in r]
                if good:
                    warm = good[1:] or good
                    key = f"{model}{' (cpu)' if cpu_only else ' (gpu)'}"
                    results["summary"][key] = {
                        "cold_ttft_ms": good[0]["ttft_ms"],
                        "cold_total_ms": good[0]["total_ms"],
                        "load_ms": good[0]["load_ms"],
                        "warm_ttft_ms_median": statistics.median(r["ttft_ms"] for r in warm),
                        "warm_total_ms_median": statistics.median(r["total_ms"] for r in warm),
                        "tok_s_median": statistics.median(r["tok_s"] for r in warm),
                        "vram_bytes": (good[0].get("loaded") or [{}])[0].get("size_vram"),
                    }
            await unload(client, model)
        results["embedding"] = await embed_runs(client, "nomic-embed-text", args.warm)
        print(results["embedding"])
    results["provider"] = await provider_check(args.models.split(",")[0])
    print(results["provider"])
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n| model | cold TTFT | cold total | load | warm TTFT | warm total | tok/s | VRAM |")
    print("|---|---|---|---|---|---|---|---|")
    for key, s in results["summary"].items():
        vram = f"{(s['vram_bytes'] or 0) / 2**20:.0f} MiB"
        print(
            f"| {key} | {s['cold_ttft_ms']} ms | {s['cold_total_ms']} ms | {s['load_ms']} ms | "
            f"{s['warm_ttft_ms_median']:.0f} ms | {s['warm_total_ms_median']:.0f} ms | "
            f"{s['tok_s_median']} | {vram} |"
        )
    print(f"\nresults: {RESULTS}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    asyncio.run(main())
