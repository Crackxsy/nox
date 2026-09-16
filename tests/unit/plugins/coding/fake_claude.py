"""Fake `claude` CLI executable for `coding` plugin tests: emits recorded stream-json NDJSON lines
(shapes recorded for real in SP-18, `11 - Spikes/SP-18 Claude Code Session Wrapper.md`; no personal
data, no real session content). Selected by the `FAKE_CLI_MODE` env var:

- "ok"          - init, one Edit tool_use touching notes.txt, a result (success).
- "always_fail" - init, one Edit tool_use, a result with `is_error=True` and a generic (repairable)
                   reason - used to drive the repair loop to its cap.
- "max_turns"   - init, a result shaped like a real `--max-turns` exhaustion
                   (`subtype=error_max_turns`, `terminal_reason=max_turns`) - must NOT be treated as
                   repairable.
- "logged_out"  - init, a result reporting the CLI is not logged in - must NOT be treated as
                   repairable either.
- "hang"        - init, one Edit tool_use, then sleeps; writes its own pid to `FAKE_CLI_PIDFILE` so
                   a test can assert the process is actually gone after cancel/kill-switch.

`--version` is answered directly, matching the real CLI's `-p`-independent behavior.
"""

import json
import os
import sys
import time

args = sys.argv[1:]
if "--version" in args:
    print("9.9.9 (fake)")
    sys.exit(0)

mode = os.environ.get("FAKE_CLI_MODE", "ok")
session_id = os.environ.get("FAKE_CLI_SESSION_ID", "fake-session-1")
pidfile = os.environ.get("FAKE_CLI_PIDFILE")
if pidfile:
    with open(pidfile, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

# Drain the prompt from stdin like the real CLI (avoids the child blocking the parent's write()).
sys.stdin.read()


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


emit(
    {
        "type": "system",
        "subtype": "init",
        "model": "fake-model",
        "session_id": session_id,
        "permissionMode": "acceptEdits",
        "tools": ["Read", "Edit", "Write", "Glob", "Grep"],
    }
)

if mode == "logged_out":
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "session_id": session_id,
            "result": "Not logged in · Please run /login",
            "usage": {},
        }
    )
    sys.exit(0)

if mode == "max_turns":
    emit(
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "session_id": session_id,
            "stop_reason": "tool_use",
            "terminal_reason": "max_turns",
            "result": "",
            "num_turns": 1,
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
    )
    sys.exit(0)

# "ok" / "always_fail" / "hang" all emit the same tool_use first.
emit(
    {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Edit", "input": {}},
        },
        "session_id": session_id,
    }
)
emit(
    {
        "type": "assistant",
        "session_id": session_id,
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Edit",
                    "input": {"file_path": "notes.txt", "old_string": "a", "new_string": "b"},
                }
            ],
        },
    }
)
emit(
    {
        "type": "user",
        "session_id": session_id,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "updated",
                    "is_error": False,
                }
            ],
        },
    }
)

if mode == "hang":
    time.sleep(30)
    sys.exit(0)

if mode == "always_fail":
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "session_id": session_id,
            "result": "2 tests failing in test_notes.py",
            "num_turns": 2,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }
    )
    sys.exit(0)

# "ok"
emit(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": session_id,
        "result": "Done: added a line to notes.txt",
        "total_cost_usd": 0.01,
        "duration_ms": 50,
        "num_turns": 2,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
)
