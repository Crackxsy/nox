"""A stand-in for `claude -p --input-format stream-json`: same lines in, same lines out.

Driven by environment variables so a test can shape it:
FAKE_LOG          JSONL file every received message is appended to (plus `pid`/`child` lines)
FAKE_INIT_DELAY   seconds the `initialize` control request takes
FAKE_CHILD        "1": start a long-lived child process at `initialize`, like the MCP bridge
FAKE_DELTA_DELAY  seconds between text deltas (a slow answer, for barge-in and kill tests)
A user message containing "TOOL" is answered with text, a tool_use block, then more text.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

LOG = os.environ.get("FAKE_LOG", "")
COST_PER_TURN = 0.01


def log(entry: dict[str, object]) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


_WRITE = threading.Lock()
_INTERRUPTED = threading.Event()


def out(entry: dict[str, object]) -> None:
    with _WRITE:
        sys.stdout.write(json.dumps(entry) + "\n")
        sys.stdout.flush()


def _reader(inbox: queue.Queue[dict[str, object] | None]) -> None:
    """Interrupts are handled here at once, even while the main loop is mid-answer."""
    for raw in sys.stdin:
        message = json.loads(raw)
        request = message.get("request") if message.get("type") == "control_request" else None
        if isinstance(request, dict) and request.get("subtype") == "interrupt":
            log({"in": message})
            _INTERRUPTED.set()
            out(
                {
                    "type": "control_response",
                    "response": {"subtype": "success", "request_id": message["request_id"]},
                }
            )
            continue
        inbox.put(message)
    inbox.put(None)


def delta(text: str) -> None:
    out(
        {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        }
    )


def main() -> None:
    # The real CLI speaks UTF-8 on its pipes whatever the console code page is.
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    model = next((a for i, a in enumerate(sys.argv) if sys.argv[i - 1] == "--model"), "default")
    log({"pid": os.getpid(), "model": model})
    turns = 0
    inbox: queue.Queue[dict[str, object] | None] = queue.Queue()
    threading.Thread(target=_reader, args=(inbox,), daemon=True).start()
    while True:
        message = inbox.get()
        if message is None:
            return
        log({"in": message})
        if message.get("type") == "control_request":
            request = message["request"]
            assert isinstance(request, dict)
            if request.get("subtype") == "initialize":
                if os.environ.get("FAKE_CHILD") == "1":
                    child = subprocess.Popen(  # noqa: S603 - fixed argv
                        [sys.executable, "-c", "import time; time.sleep(600)"]
                    )
                    log({"child": child.pid})
                time.sleep(float(os.environ.get("FAKE_INIT_DELAY", "0")))
            if request.get("subtype") == "set_model":
                model = request["model"]
                log({"model": model})
            out(
                {
                    "type": "control_response",
                    "response": {"subtype": "success", "request_id": message["request_id"]},
                }
            )
            continue
        turns += 1
        _INTERRUPTED.clear()
        body = message["message"]
        assert isinstance(body, dict)
        content = str(body["content"])
        out({"type": "system", "subtype": "init", "model": model, "session_id": "s"})
        words = ["Hallo", " Welt."]
        for word in words:
            delta(word)
            time.sleep(float(os.environ.get("FAKE_DELTA_DELAY", "0")))
        if "TOOL" in content:
            out(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "content_block": {"type": "tool_use", "name": "mcp__nox__home-light"},
                    },
                }
            )
            delta("Erledigt.")
        if "SLOW" in content:
            for _ in range(200):
                if _INTERRUPTED.is_set():
                    break
                delta(" weiter")
                time.sleep(0.05)
        out(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "",
                "total_cost_usd": COST_PER_TURN * turns,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        )


if __name__ == "__main__":
    main()
