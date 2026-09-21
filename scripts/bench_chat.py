"""Measure the felt latency of a chat turn against a real, booted core.

Boots `NoxCore(voice=False)` headless on free ports with a temporary data directory and a small
seeded vault, then drives a fixed set of German prompts through the orchestrator exactly the way
the dashboard does - retrieval, system prompt, router, provider, streaming - and reports, per
prompt and per stage: retrieval, prompt build, time to first token, time to the first complete
sentence (which is when sentence-wise speech can start), total, tokens per second, and which
provider answered.

Two runs make a comparison:

    python scripts/bench_chat.py --baseline --label before --json bench-before.json
    python scripts/bench_chat.py            --label after  --json bench-after.json

`--baseline` restores the behaviour the optimisations replaced (no deterministic fast path, no
escalation, the old 4000-token retrieval budget with no relevance floor, no query-embedding
cache), so both numbers come from the same binary and the same machine on the same day.

`--compare-models a,b,c` additionally runs the same prompts through the same router against
several local models and writes every answer to the JSON, so the answers can be judged and not
just timed.

No personal data: the prompts, the seeded vault notes and the temporary paths are all synthetic.

Exit codes: 0 success, 1 bad usage or configuration, 2 the core did not boot, 3 no provider
answered a single prompt (the numbers would be meaningless).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from nox.ai.base import AiRequest, AiRole, Message  # noqa: E402
from nox.ai.escalation import EscalationPolicy  # noqa: E402
from nox.ai.fastpath import FastPath  # noqa: E402
from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore  # noqa: E402
from nox.core.config import NoxConfig, load_config  # noqa: E402
from nox.core.events import E, Event  # noqa: E402
from nox.memory.install import MemoryRuntime  # noqa: E402

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_CORE = 2
EXIT_NO_ANSWER = 3

START_TIMEOUT_S = 120
STOP_TIMEOUT_S = 30
TURN_TIMEOUT_S = 180

#: The retrieval settings this script's baseline mode restores: the shipped defaults before the
#: latency work (`retrieval_max_tokens: 4000`, no relevance floor).
BASELINE_MAX_TOKENS = 4000
BASELINE_MIN_SCORE = 0.0
#: The full cosine range, i.e. no margin at all - every hit the search returns is kept.
BASELINE_SCORE_MARGIN = 2.0


@dataclass(frozen=True, slots=True)
class Prompt:
    category: str
    text: str


#: Fifteen prompts covering what a companion is actually asked: greetings, phatic replies, a tool
#: request, short factual questions, a two-sentence conversation, two questions that can only be
#: answered from the seeded vault, one deliberately long answer, and one question a 3B model
#: answers badly (the escalation candidate).
PROMPTS: tuple[Prompt, ...] = (
    Prompt("greeting", "Hallo"),
    Prompt("greeting", "Guten Morgen"),
    Prompt("chitchat", "Wie geht es dir?"),
    Prompt("chitchat", "Danke"),
    Prompt("tool", "Wie spät ist es?"),
    Prompt("tool", "Welcher Tag ist heute?"),
    Prompt("fact", "Wofür steht die Abkürzung SQL?"),
    Prompt("fact", "Was ist der Unterschied zwischen einer Liste und einem Tupel in Python?"),
    Prompt("fact", "Nenne drei Vorteile von Versionskontrolle."),
    Prompt("chat", "Erklär mir kurz, was ein Vektorindex ist."),
    Prompt("chat", "Ich habe den ganzen Tag an einem Import gearbeitet und es lief nicht gut."),
    Prompt("memory", "Welche Datenbank verwendet das Projekt laut meinen Notizen?"),
    Prompt("memory", "Was steht in meinen Notizen zum Backup-Konzept?"),
    Prompt(
        "long",
        "Erkläre mir ausführlich in mindestens acht Sätzen, wie ein System funktioniert, "
        "das Antworten mit abgerufenen Dokumenten anreichert.",
    ),
    Prompt(
        "hard",
        "Meine nächtliche Sicherung bricht seit dem letzten Update jedes Mal nach ungefähr "
        "zwanzig Minuten ohne Fehlermeldung ab, obwohl genügend Speicherplatz frei ist und die "
        "Platte sauber eingebunden bleibt; was sind die drei plausibelsten Ursachen dafür und "
        "wie prüfe ich sie der Reihe nach?",
    ),
)

#: The cold-start probe. It must reach a model, so it is deliberately not something the fast path
#: answers: the number it produces is "what the first real question after an idle period costs".
COLD_START_PROMPT = Prompt("fact", "Was macht ein Compiler?")

#: Synthetic vault notes so the two memory prompts have something true to retrieve, and so the
#: other prompts have something plausible but off-topic to *not* retrieve.
VAULT_NOTES: dict[str, str] = {
    "projekt.md": (
        "# Projekt\n\n"
        "Das Projekt speichert seine Daten in einer lokalen SQLite-Datenbank. Für die semantische "
        "Suche liegt daneben ein Vektorindex, der die Notizen in Abschnitte zerlegt und als "
        "Vektoren ablegt. Die Antwortzeit einer Anfrage soll unter einer Sekunde bleiben.\n"
    ),
    "backup.md": (
        "# Backup-Konzept\n\n"
        "Jede Nacht um 03:00 Uhr läuft eine inkrementelle Sicherung auf eine externe Platte. "
        "Sonntags läuft zusätzlich eine Vollsicherung. Sicherungen werden 30 Tage aufbewahrt, "
        "danach werden die ältesten automatisch gelöscht. Vor jeder Vollsicherung wird geprüft, "
        "ob die Zielplatte eingebunden ist.\n"
    ),
    "kochen.md": (
        "# Nudelteig\n\n"
        "Für frischen Nudelteig kommen auf 100 Gramm Mehl ein Ei und eine Prise Salz. Der Teig "
        "ruht mindestens 30 Minuten im Kühlschrank, bevor er ausgerollt wird.\n"
    ),
    "gitarre.md": (
        "# Gitarre stimmen\n\n"
        "Die Standardstimmung einer sechssaitigen Gitarre ist von der tiefsten zur höchsten "
        "Saite E A D G H E. Ein Stimmgerät zeigt die Abweichung in Cent an.\n"
    ),
}


@dataclass
class TurnMeasurement:
    """One prompt, once. Every field is measured; nothing here is estimated except `tokens_out`
    when the provider reports no usage, which is then marked by `tokens_estimated`."""

    index: int
    category: str
    prompt: str
    provider: str = ""
    degraded: bool = False
    fast_path: str = ""
    escalated: str = ""
    retrieval_ms: float = 0.0
    prompt_build_ms: float = 0.0
    first_token_ms: float = 0.0
    first_sentence_ms: float = 0.0
    total_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_estimated: bool = False
    tokens_per_s: float = 0.0
    answer: str = ""
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


@dataclass
class ModelMeasurement:
    """One prompt against one named model, straight through the router."""

    model: str
    category: str
    prompt: str
    provider: str = ""
    first_token_ms: float = 0.0
    total_ms: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_per_s: float = 0.0
    answer: str = ""
    error: str = ""


def _free_port_base(attempts: int = 50) -> int:
    """A port `base` such that both `base` and `base + 1` were bindable a moment ago.

    The OS picks it. A random port regularly lands inside a Hyper-V excluded range on Windows,
    where binding fails even though nothing listens there.
    """
    last: OSError | None = None
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            base = int(probe.getsockname()[1])
        if base + 1 > 65535:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second:
                second.bind(("127.0.0.1", base + 1))
        except OSError as exc:
            last = exc
            continue
        return base
    raise RuntimeError(f"no free port pair after {attempts} attempts") from last


def _config(tmp_dir: Path, *, privacy_mode: str) -> NoxConfig:
    """The same headless configuration the integration tests boot, on OS-granted ports."""
    base = _free_port_base()
    vault = tmp_dir / "vault"
    vault.mkdir(parents=True)
    for name, body in VAULT_NOTES.items():
        (vault / name).write_text(body, encoding="utf-8")
    overrides = {
        "paths": {
            "data_dir": str(tmp_dir / "data"),
            "vault_dir": str(vault),
            "index_dir": str(tmp_dir / "index"),
            "database_dir": str(tmp_dir / "db"),
            "cache_dir": str(tmp_dir / "cache"),
            "backups_dir": str(tmp_dir / "backups"),
            "runtime_dir": str(tmp_dir / "runtime"),
            "logs_dir": str(tmp_dir / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": privacy_mode},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
    }
    return load_config(DEFAULTS_PATH, None, None, overrides)


class _UsageCollector:
    """Reads the router's own `ai.response_ready` events, so token counts are the provider's."""

    def __init__(self, core: NoxCore) -> None:
        assert core.bus is not None
        self._unsub = core.bus.subscribe(E.AI_RESPONSE_READY, self._on_ready)
        self._by_request: dict[str, dict[str, Any]] = {}

    def _on_ready(self, event: Event) -> None:
        request_id = str(event.payload.get("request_id", ""))
        if request_id:
            self._by_request[request_id] = dict(event.payload)

    def take(self, request_id: str) -> dict[str, Any]:
        return self._by_request.pop(request_id, {})

    def close(self) -> None:
        self._unsub()


def _memory(core: NoxCore) -> MemoryRuntime | None:
    """The memory extension's runtime, or None when it failed to install."""
    runtime = core.extensions.get("memory")
    return runtime if isinstance(runtime, MemoryRuntime) else None


def _apply_baseline(core: NoxCore) -> None:
    """Undo the latency work for this process, so `--baseline` measures the previous behaviour."""
    assert core.orchestrator is not None
    core.orchestrator.fast_path = FastPath(enabled=False)
    core.orchestrator.escalation = EscalationPolicy(enabled=False)
    memory = _memory(core)
    if memory is not None:
        memory.retrieval.set_budget(
            max_tokens=BASELINE_MAX_TOKENS,
            min_score=BASELINE_MIN_SCORE,
            score_margin=BASELINE_SCORE_MARGIN,
        )
        memory.embeddings.set_query_cache_size(0)


async def _await_vault_index(core: NoxCore) -> None:
    """The memory prompts are only meaningful once the seeded notes are indexed."""
    memory = _memory(core)
    if memory is None or memory.scan_task is None:
        return
    await asyncio.wait_for(asyncio.shield(memory.scan_task), timeout=START_TIMEOUT_S)


def _retrieved_rows(core: NoxCore) -> list[dict[str, Any]]:
    """What retrieval put into the prompt for the turn that just ran, for judging relevance."""
    memory = _memory(core)
    if memory is None or memory.prompt_adapter is None:
        return []
    result = memory.prompt_adapter.last_result
    if result is None:
        return []
    return [
        {"source": item.source, "score": round(item.score, 3), "via": item.via, "kind": item.kind}
        for item in result.items
    ]


async def _run_turn(
    core: NoxCore, usage: _UsageCollector, index: int, prompt: Prompt
) -> TurnMeasurement:
    assert core.orchestrator is not None
    measurement = TurnMeasurement(index=index, category=prompt.category, prompt=prompt.text)
    try:
        turn = await asyncio.wait_for(
            core.orchestrator.handle_text(prompt.text, speak=False), timeout=TURN_TIMEOUT_S
        )
    except TimeoutError:
        measurement.error = f"no answer within {TURN_TIMEOUT_S}s"
        return measurement
    except Exception as exc:  # noqa: BLE001 - a failed prompt is a reported row, not a crash
        measurement.error = f"{type(exc).__name__}: {exc}"
        return measurement
    reported = usage.take(turn.request_id)
    measurement.provider = turn.provider
    measurement.degraded = turn.degraded
    measurement.fast_path = turn.fast_path
    measurement.escalated = turn.escalated
    measurement.retrieval_ms = round(turn.timings.context_ms, 1)
    measurement.prompt_build_ms = round(turn.timings.prompt_build_ms, 1)
    measurement.first_token_ms = round(turn.timings.first_token_ms, 1)
    measurement.first_sentence_ms = round(turn.timings.first_sentence_ms, 1)
    measurement.total_ms = round(turn.timings.total_ms, 1)
    measurement.answer = turn.response
    measurement.retrieved = _retrieved_rows(core)
    tokens_out = reported.get("tokens_out")
    if isinstance(tokens_out, int) and tokens_out > 0:
        measurement.tokens_out = tokens_out
    else:
        measurement.tokens_out = max(1, len(turn.response) // 4)
        measurement.tokens_estimated = True
    tokens_in = reported.get("tokens_in")
    measurement.tokens_in = tokens_in if isinstance(tokens_in, int) else 0
    if turn.fast_path:
        # Nothing was generated, so there is no generation rate to report; the total says it all.
        measurement.tokens_per_s = 0.0
    else:
        generate_ms = max(1.0, measurement.total_ms - measurement.first_token_ms)
        measurement.tokens_per_s = round(measurement.tokens_out / (generate_ms / 1000.0), 1)
    return measurement


async def _run_model_comparison(
    core: NoxCore, models: list[str], prompts: tuple[Prompt, ...]
) -> list[ModelMeasurement]:
    """Same router, same system prompt, one model per pass - latency *and* the answers."""
    assert core.router is not None and core.orchestrator is not None
    system_prompt = core.orchestrator.system_prompt()
    results: list[ModelMeasurement] = []
    for model in models:
        for prompt in prompts:
            row = ModelMeasurement(model=model, category=prompt.category, prompt=prompt.text)
            request = AiRequest(
                request_id=f"bench-{model}-{prompt.category}-{len(results)}",
                role=AiRole.CHAT,
                messages=[
                    Message(role="system", content=system_prompt),
                    Message(role="user", content=prompt.text),
                ],
                privacy_mode="offline",
                max_tokens=400,
                timeout_s=TURN_TIMEOUT_S,
                metadata={"language": "de", "model": model},
            )
            started = time.perf_counter()
            first: float | None = None
            parts: list[str] = []
            try:
                async for chunk in core.router.stream(request):
                    if chunk.delta:
                        if first is None:
                            first = (time.perf_counter() - started) * 1000
                        parts.append(chunk.delta)
            except Exception as exc:  # noqa: BLE001 - a model that fails is a reported row
                row.error = f"{type(exc).__name__}: {exc}"
                results.append(row)
                continue
            row.total_ms = round((time.perf_counter() - started) * 1000, 1)
            row.first_token_ms = round(first or 0.0, 1)
            row.answer = "".join(parts)
            row.provider = core.router.explain(request.request_id).splitlines()[-1].strip(" =>")
            row.tokens_out = max(1, len(row.answer) // 4)
            generate_ms = max(1.0, row.total_ms - row.first_token_ms)
            row.tokens_per_s = round(row.tokens_out / (generate_ms / 1000.0), 1)
            results.append(row)
    return results


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def _summarize(rows: list[TurnMeasurement]) -> dict[str, Any]:
    answered = [r for r in rows if not r.error]
    by_category: dict[str, dict[str, float]] = {}
    for category in sorted({r.category for r in answered}):
        group = [r for r in answered if r.category == category]
        by_category[category] = {
            "turns": len(group),
            "first_token_ms": _median([r.first_token_ms for r in group]),
            "first_sentence_ms": _median([r.first_sentence_ms for r in group]),
            "total_ms": _median([r.total_ms for r in group]),
            "retrieval_ms": _median([r.retrieval_ms for r in group]),
        }
    return {
        "turns": len(rows),
        "answered": len(answered),
        "failed": len(rows) - len(answered),
        "median_retrieval_ms": _median([r.retrieval_ms for r in answered]),
        "median_prompt_build_ms": _median([r.prompt_build_ms for r in answered]),
        "median_first_token_ms": _median([r.first_token_ms for r in answered]),
        "median_first_sentence_ms": _median([r.first_sentence_ms for r in answered]),
        "median_total_ms": _median([r.total_ms for r in answered]),
        "median_tokens_per_s": _median([r.tokens_per_s for r in answered]),
        "providers": sorted({r.provider for r in answered if r.provider}),
        "fast_path_turns": sum(1 for r in answered if r.fast_path),
        "escalated_turns": sum(1 for r in answered if r.escalated),
        "by_category": by_category,
    }


_COLUMNS = (
    ("#", 3),
    ("category", 9),
    ("prompt", 38),
    ("provider", 10),
    ("retr", 7),
    ("build", 6),
    ("ttft", 8),
    ("1st sent", 9),
    ("total", 9),
    ("tok_in", 7),
    ("tok/s", 6),
)


def _print_table(rows: list[TurnMeasurement], summary: dict[str, Any], label: str) -> None:
    header = " ".join(name.ljust(width) for name, width in _COLUMNS)
    print(f"\nchat turns ({label})")
    print(header)
    print("-" * len(header))
    for row in rows:
        prompt = row.prompt if len(row.prompt) <= 38 else row.prompt[:35] + "..."
        provider = row.provider or ("FAILED" if row.error else "-")
        cells = (
            str(row.index),
            row.category,
            prompt,
            provider,
            f"{row.retrieval_ms:.0f}",
            f"{row.prompt_build_ms:.1f}",
            f"{row.first_token_ms:.0f}",
            f"{row.first_sentence_ms:.0f}",
            f"{row.total_ms:.0f}",
            str(row.tokens_in),
            f"{row.tokens_per_s:.1f}",
        )
        print(" ".join(cell.ljust(width) for cell, (_, width) in zip(cells, _COLUMNS, strict=True)))
        if row.error:
            print(f"    error: {row.error}")
    print(
        f"\nmedian  retrieval {summary['median_retrieval_ms']} ms | "
        f"prompt build {summary['median_prompt_build_ms']} ms | "
        f"first token {summary['median_first_token_ms']} ms | "
        f"first sentence {summary['median_first_sentence_ms']} ms | "
        f"total {summary['median_total_ms']} ms | "
        f"{summary['median_tokens_per_s']} tok/s"
    )
    print(
        f"providers {', '.join(summary['providers']) or 'none'} | "
        f"fast path {summary['fast_path_turns']}/{summary['answered']} turns | "
        f"escalated {summary['escalated_turns']}"
    )
    for category, stats in summary["by_category"].items():
        print(
            f"  {category:<9} n={stats['turns']:<2} ttft {stats['first_token_ms']:>8.0f} ms | "
            f"first sentence {stats['first_sentence_ms']:>8.0f} ms | "
            f"total {stats['total_ms']:>8.0f} ms | retrieval {stats['retrieval_ms']:>6.0f} ms"
        )


def _print_context(rows: list[TurnMeasurement]) -> None:
    print("\nretrieved context per prompt (judge relevance here)")
    for row in rows:
        if not row.retrieved:
            print(f"  [{row.index}] {row.category:<9} {row.prompt[:50]!r}: nothing retrieved")
            continue
        joined = ", ".join(f"{r['source']}@{r['score']}/{r['via']}" for r in row.retrieved)
        print(f"  [{row.index}] {row.category:<9} {row.prompt[:50]!r}: {joined}")


def _print_models(rows: list[ModelMeasurement]) -> None:
    print("\nmodel comparison (same prompts, same router)")
    print(f"{'model':<16} {'category':<9} {'ttft':>8} {'total':>9} {'tok/s':>7}  prompt")
    for row in rows:
        if row.error:
            print(f"{row.model:<16} {row.category:<9} {'FAILED':>8}  {row.error}")
            continue
        print(
            f"{row.model:<16} {row.category:<9} {row.first_token_ms:>8.0f} "
            f"{row.total_ms:>9.0f} {row.tokens_per_s:>7.1f}  {row.prompt[:40]}"
        )
    for model in sorted({r.model for r in rows}):
        ok = [r for r in rows if r.model == model and not r.error]
        if not ok:
            print(f"  {model:<16} no successful run")
            continue
        print(
            f"  {model:<16} median ttft {_median([r.first_token_ms for r in ok]):>7.0f} ms | "
            f"median total {_median([r.total_ms for r in ok]):>8.0f} ms | "
            f"median {_median([r.tokens_per_s for r in ok]):>5.1f} tok/s"
        )


async def _bench(args: argparse.Namespace, tmp_dir: Path) -> tuple[int, dict[str, Any]]:
    config = _config(tmp_dir, privacy_mode=args.privacy_mode)
    core = NoxCore(config, voice=False, profiles_dir=PROFILES_DIR)
    try:
        await asyncio.wait_for(core.start(), timeout=START_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - reported with an exit code, not a traceback
        print(f"core did not boot: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_NO_CORE, {}
    payload: dict[str, Any] = {}
    try:
        await _await_vault_index(core)
        if args.baseline:
            _apply_baseline(core)
        prompts = (
            tuple(p for p in PROMPTS if p.category == args.category) if args.category else PROMPTS
        )
        prompts = prompts[: args.limit] if args.limit else prompts
        if not prompts:
            print(f"no prompt has category {args.category!r}", file=sys.stderr)
            return EXIT_USAGE, {}
        rows: list[TurnMeasurement] = []
        cold: TurnMeasurement | None = None
        if args.cold_first:
            cold = await _run_turn(core, _UsageCollector(core), 0, COLD_START_PROMPT)
            print(
                f"cold start ({COLD_START_PROMPT.text!r}): first token "
                f"{cold.first_token_ms:.0f} ms, total {cold.total_ms:.0f} ms"
            )
        usage = _UsageCollector(core)
        try:
            for repetition in range(args.runs):
                for index, prompt in enumerate(prompts, start=1):
                    row = await _run_turn(core, usage, index, prompt)
                    row.index = index + repetition * len(prompts)
                    rows.append(row)
        finally:
            usage.close()
        summary = _summarize(rows)
        _print_table(rows, summary, args.label)
        _print_context(rows)
        models: list[ModelMeasurement] = []
        if args.compare_models:
            models = await _run_model_comparison(
                core, args.compare_models, prompts[: args.compare_limit]
            )
            _print_models(models)
        payload = {
            "label": args.label,
            "baseline": args.baseline,
            "privacy_mode": args.privacy_mode,
            "runs": args.runs,
            "summary": summary,
            "cold_start": asdict(cold) if cold is not None else None,
            "turns": [asdict(row) for row in rows],
            "models": [asdict(row) for row in models],
        }
        if summary["answered"] == 0:
            print("no prompt was answered by any provider", file=sys.stderr)
            return EXIT_NO_ANSWER, payload
        return EXIT_OK, payload
    finally:
        await asyncio.wait_for(core.stop(), timeout=STOP_TIMEOUT_S)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bench_chat",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--label", default="run", help="name for this run in the output and JSON")
    parser.add_argument("--json", type=Path, help="write the full measurement to this file")
    parser.add_argument("--runs", type=int, default=1, help="repetitions of the whole prompt set")
    parser.add_argument("--limit", type=int, default=0, help="use only the first N prompts")
    parser.add_argument(
        "--category",
        default="",
        help="run only the prompts of this category (greeting, chitchat, tool, fact, chat, "
        "memory, long, hard)",
    )
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="measure the behaviour before the latency work (see the module docstring)",
    )
    parser.add_argument(
        "--cold-first",
        action="store_true",
        help="run one extra turn first and report it separately as the cold-start number",
    )
    parser.add_argument(
        "--privacy-mode",
        default="offline",
        choices=("offline", "private", "balanced", "full"),
        help="offline (the default) keeps the run local: cloud providers are filtered out",
    )
    parser.add_argument(
        "--compare-models",
        default="",
        help="comma-separated local model names to benchmark against each other",
    )
    parser.add_argument(
        "--compare-limit",
        type=int,
        default=6,
        help="how many prompts each compared model answers",
    )
    args = parser.parse_args(argv)
    args.compare_models = [m.strip() for m in args.compare_models.split(",") if m.strip()]
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.limit < 0:
        parser.error("--limit must not be negative")
    return args


def main(argv: list[str] | None = None) -> int:
    # The prompts and the vault notes are German; a console in a legacy code page would mangle the
    # table and hide which prompt a row belongs to.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="nox-bench-") as tmp:
        try:
            code, payload = asyncio.run(_bench(args, Path(tmp)))
        except KeyboardInterrupt:
            print("interrupted", file=sys.stderr)
            return EXIT_USAGE
        if args.json and payload:
            args.json.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")
            print(f"\nwrote {args.json}")
        return code


if __name__ == "__main__":
    raise SystemExit(main())
