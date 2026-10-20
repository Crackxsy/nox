"""Spike: can Claude Code call Nox's tools natively, from one persistent process?

Questions (Claude Code 2.x, headless):
1. Does `--tools ""` leave MCP tools from `--mcp-config` usable?
2. Does `--safe-mode` still load an explicit `--mcp-config` (with `--strict-mcp-config`)?
3. How long may one MCP tool call take before the CLI gives up (must be >= 60 s, the
   confirmation window), and does `MCP_TOOL_TIMEOUT` raise it?

Also measures, per turn of one long-lived `--input-format stream-json` process, the time to the
first text token and to the first tool call, against a fresh `claude -p` per turn.

Usage: python spikes/sp13_native_tools.py [--slow 70] [--turns 5] [--model sonnet]
Runs headless (no console window) and prints JSON results.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXTRA: list[str] = []
CWD: list[str] = []
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a normal Nox core would hand the CLI: none of an outer session's vars."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDE", "CLAUDECODE")) and k not in {"AI_AGENT"}
    }
    env.update(extra or {})
    return env


def mcp_config(path: Path, log: Path) -> Path:
    cfg = {
        "mcpServers": {
            "nox": {
                "command": sys.executable,
                "args": [str(HERE / "sp13_mcp_echo_server.py")],
                "env": {"SPIKE_LOG": str(log)},
            }
        }
    }
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


class Persistent:
    def __init__(self, args: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=CWD[0] if CWD else None,
            creationflags=NO_WINDOW,
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _err(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr.append(line)

    def turn(self, text: str, timeout_s: float = 240) -> dict[str, object]:
        assert self.proc.stdin is not None
        started = time.perf_counter()
        msg = {"type": "user", "message": {"role": "user", "content": text}}
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        first_token = first_tool = None
        tools: list[str] = []
        texts: list[str] = []
        init: dict[str, object] = {}
        thinking = False
        while True:
            line = self.lines.get(timeout=timeout_s)
            if line is None:
                return {"error": "process ended", "stderr": "".join(self.stderr)[-800:]}
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            now = (time.perf_counter() - started) * 1000
            if kind == "system" and data.get("subtype") == "init":
                init = {
                    "tools": data.get("tools"),
                    "mcp_servers": data.get("mcp_servers"),
                    "model": data.get("model"),
                    "plugins": data.get("plugins"),
                    "skills": len(data.get("skills") or []),
                    "slash_commands": len(data.get("slash_commands") or []),
                    "agents": len(data.get("agents") or []),
                    "fast_mode_state": data.get("fast_mode_state"),
                    "fast_mode_disabled_reason": data.get("fast_mode_disabled_reason"),
                    "per_turn_effort_active": data.get("per_turn_effort_active"),
                    "memory_paths": data.get("memory_paths"),
                }
            if kind == "stream_event":
                ev = data.get("event") or {}
                if ev.get("type") == "content_block_start":
                    block = ev.get("content_block") or {}
                    if block.get("type") == "thinking":
                        thinking = True
                    if block.get("type") == "tool_use":
                        tools.append(str(block.get("name")))
                        if first_tool is None:
                            first_tool = now
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        texts.append(str(delta.get("text", "")))
                        if first_token is None:
                            first_token = now
            if kind == "result":
                return {
                    "first_token_ms": first_token,
                    "first_tool_ms": first_tool,
                    "total_ms": now,
                    "tools": tools,
                    "text": "".join(texts)[-300:],
                    "is_error": data.get("is_error"),
                    "subtype": data.get("subtype"),
                    "init": init,
                    "thinking": thinking,
                }

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def base_args(model: str, cfg: Path, *, safe: bool, persistent: bool) -> list[str]:
    args = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--no-session-persistence",
        "--tools",
        "",
        "--mcp-config",
        str(cfg),
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__nox__*",
        "--permission-prompts",
        "none",
        "--model",
        model,
        "--system-prompt",
        "You are a terse test assistant. Use the nox tools when asked. Answer in one sentence.",
    ]
    if persistent:
        args += ["--input-format", "stream-json"]
    if safe:
        args.append("--safe-mode")
    return args + EXTRA


def resolve(args: list[str]) -> list[str]:
    import shutil

    exe = shutil.which(args[0])
    assert exe, "claude not on PATH"
    return [exe, *args[1:]]


def one_shot(args: list[str], prompt: str, env: dict[str, str]) -> dict[str, object]:
    started = time.perf_counter()
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        creationflags=NO_WINDOW,
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(prompt)
    proc.stdin.close()
    first_token = None
    for line in proc.stdout:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "stream_event":
            delta = (data.get("event") or {}).get("delta") or {}
            if delta.get("type") == "text_delta" and first_token is None:
                first_token = (time.perf_counter() - started) * 1000
        if data.get("type") == "result":
            break
    proc.wait(timeout=30)
    return {"first_token_ms": first_token, "total_ms": (time.perf_counter() - started) * 1000}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--turns", type=int, default=5)
    parser.add_argument("--slow", type=float, default=0)
    parser.add_argument("--tool-timeout-ms", default="")
    parser.add_argument("--skip-oneshot", action="store_true")
    parser.add_argument("--only-plain", action="store_true")
    parser.add_argument("--extra", default="", help="extra CLI args, space separated")
    parser.add_argument("--env", action="append", default=[], help="K=V for the CLI env")
    parser.add_argument("--probe", default="", help="one extra prompt to run after the tool turn")
    parser.add_argument("--isolated-cwd", action="store_true")
    ns = parser.parse_args()
    EXTRA[:] = ["" if a == "EMPTY" else a for a in ns.extra.split(" ") if a]
    out: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        log = tmp_path / "calls.log"
        cfg = mcp_config(tmp_path / "mcp.json", log)
        if ns.isolated_cwd:
            (tmp_path / "cwd").mkdir()
            CWD[:] = [str(tmp_path / "cwd")]
        extra = {"MCP_TOOL_TIMEOUT": ns.tool_timeout_ms} if ns.tool_timeout_ms else {}
        extra.update(dict(kv.split("=", 1) for kv in ns.env))
        env = clean_env(extra)
        for safe in (False,) if ns.only_plain else (False, True):
            label = "safe" if safe else "plain"
            spawn = time.perf_counter()
            p = Persistent(resolve(base_args(ns.model, cfg, safe=safe, persistent=True)), env)
            first = p.turn("Call the echo tool with text 'ping', then report what it returned.")
            first["spawn_to_result_ms"] = (time.perf_counter() - spawn) * 1000
            out[f"{label}.tool_turn"] = first
            if ns.probe:
                out[f"{label}.probe"] = p.turn(ns.probe)
            if not safe:
                chats = [
                    p.turn(f"Say a short German greeting number {i}.") for i in range(ns.turns)
                ]
                out["persistent.chat_thinking"] = [c.get("thinking") for c in chats]
                tool_turns = [
                    p.turn(f"Call the echo tool with text 'x{i}' and tell me the result.")
                    for i in range(ns.turns)
                ]
                ft = [float(c["first_token_ms"]) for c in chats if c.get("first_token_ms")]
                tt = [float(c["first_tool_ms"]) for c in tool_turns if c.get("first_tool_ms")]
                out["persistent.chat_first_token_ms"] = ft
                out["persistent.first_tool_ms"] = tt
                out["persistent.chat_first_token_p50"] = statistics.median(ft) if ft else None
                out["persistent.first_tool_p50"] = statistics.median(tt) if tt else None
                if ns.slow:
                    out["slow_turn"] = p.turn(
                        f"Call the slow tool with seconds={ns.slow} and report its answer."
                    )
            p.close()
        out["calls_log"] = (
            log.read_text(encoding="utf-8").splitlines()[-12:] if log.exists() else []
        )
        if not ns.skip_oneshot:
            args = resolve(base_args(ns.model, cfg, safe=True, persistent=False))
            shots = [
                one_shot(args, f"Say a short German greeting number {i}.", env) for i in range(3)
            ]
            out["oneshot.first_token_ms"] = [s["first_token_ms"] for s in shots]
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
