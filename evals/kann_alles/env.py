"""The simulated world: a headless core on free ports, a sandbox folder, a fake web server.

Only public, long-standing APIs of the core are used (`NoxCore`, `load_config`, the orchestrator's
`handle_text`, the executor's `call`, the permission engine's `reply`), so the same harness measures
an older checkout as a baseline without changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from kann_alles.tasks import Environment
from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.events import E, Event
from nox.security.model import Decision

START_TIMEOUT_S = 180.0
STOP_TIMEOUT_S = 30.0
WEB_ROOT = Path(__file__).resolve().parent / "envs" / "web"


def free_port_pair(attempts: int = 50) -> int:
    """A `base` such that `base` and `base + 1` were bindable a moment ago (OS-chosen)."""
    for _ in range(attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            base = int(probe.getsockname()[1])
        if base + 1 > 65535:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as second:
                second.bind(("127.0.0.1", base + 1))
        except OSError:
            continue
        return base
    raise RuntimeError("no free port pair")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return


class FakeWeb:
    """Static pages (timetable, shop, weather) on a loopback port, served from `envs/web`."""

    def __init__(self, root: Path = WEB_ROOT) -> None:
        handler = functools.partial(_QuietHandler, directory=str(root))
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@dataclass
class ToolCallRecord:
    name: str
    ok: bool
    error: str
    started_s: float
    duration_ms: float


@dataclass
class Probe:
    """What one turn did, observed from outside: tool calls, confirmations, speech."""

    calls: list[ToolCallRecord] = field(default_factory=list)
    confirmations: int = 0
    first_say_s: float | None = None
    said: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.calls.clear()
        self.confirmations = 0
        self.first_say_s = None
        self.said.clear()


class RecordingSpeaker:
    """Stands in for the voice worker: notes when the first sentence was handed over to speech."""

    def __init__(self, probe: Probe) -> None:
        self._probe = probe

    async def say(self, request: Any) -> None:
        if self._probe.first_say_s is None:
            self._probe.first_say_s = time.perf_counter()
        self._probe.said.append(str(getattr(request, "text", "")))

    async def interrupt(self, *, reason: str) -> None:
        return


class World:
    """One booted core plus its sandbox, reused across tasks and reset between them."""

    def __init__(self, root: Path, *, privacy_mode: str = "balanced") -> None:
        self.root = root
        self.sandbox = root / "sandbox"
        self.vault = root / "vault"
        self.web = FakeWeb()
        self.probe = Probe()
        self.confirm_policy = Decision.ALLOW
        self._privacy_mode = privacy_mode
        self.core: NoxCore | None = None
        self._unsub: Callable[[], None] | None = None

    def _config(self) -> Any:
        base = free_port_pair()
        for folder in (self.sandbox, self.vault):
            folder.mkdir(parents=True, exist_ok=True)
        paths = {
            name: str(self.root / name)
            for name in ("data", "index", "db", "cache", "backups", "runtime", "logs")
        }
        overrides = {
            "paths": {
                "data_dir": paths["data"],
                "vault_dir": str(self.vault),
                "index_dir": paths["index"],
                "database_dir": paths["db"],
                "cache_dir": paths["cache"],
                "backups_dir": paths["backups"],
                "runtime_dir": paths["runtime"],
                "logs_dir": paths["logs"],
            },
            "ipc": {"port": base, "http_port": base + 1},
            "privacy": {"mode": self._privacy_mode},
            "health": {"check_interval_s": 3600},
            "logging": {"level": "WARNING"},
            # Deleting goes through the recycle bin by default; a benchmark must not fill the real
            # one, so there is no delete tool in this world (a task that needs one says so).
            "files": {"roots": [str(self.sandbox)], "delete_to_recycle_bin": False},
            "plugins": {"enabled": []},
        }
        return load_config(DEFAULTS_PATH, None, None, overrides)

    async def start(self) -> NoxCore:
        self.web.start()
        core = NoxCore(self._config(), voice=False, profiles_dir=PROFILES_DIR)
        await asyncio.wait_for(core.start(), timeout=START_TIMEOUT_S)
        self.core = core
        assert core.orchestrator is not None and core.bus is not None
        core.orchestrator.speaker = RecordingSpeaker(self.probe)
        self._wrap_executor(core)
        self._unsub = core.bus.subscribe(E.SECURITY_PERMISSION_REQUESTED, self._on_confirm)
        return core

    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
        if self.core is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.core.stop(), timeout=STOP_TIMEOUT_S)
        self.web.stop()

    def _wrap_executor(self, core: NoxCore) -> None:
        executor = core.tool_executor
        original = executor.call
        probe = self.probe

        async def recording_call(*args: Any, **kwargs: Any) -> Any:
            name = str(kwargs.get("name", args[1] if len(args) > 1 else ""))
            started = time.perf_counter()
            result = await original(*args, **kwargs)
            probe.calls.append(
                ToolCallRecord(
                    name=name,
                    ok=bool(getattr(result, "ok", False)),
                    error=str(getattr(result, "error", "") or ""),
                    started_s=started,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            )
            return result

        executor.call = recording_call

    def _on_confirm(self, event: Event) -> None:
        assert self.core is not None and self.core.security is not None
        grant_id = str(event.payload.get("request_id", ""))
        self.probe.confirmations += 1
        self.core.security.engine.reply(grant_id, self.confirm_policy, by="eval")

    # -- per task -----------------------------------------------------------------------------

    @staticmethod
    def empty_env() -> Environment:
        return Environment()

    async def prepare(
        self, env: Environment, *, confirm: str, fresh_session: bool = True
    ) -> None:
        """Reset the sandbox, seed files/notes/reminders and (by default) start a new
        conversation, so no task sees what the previous one talked about."""
        assert self.core is not None and self.core.orchestrator is not None
        self.probe.reset()
        self.confirm_policy = Decision.ALLOW if confirm == "allow" else Decision.DENY
        shutil.rmtree(self.sandbox, ignore_errors=True)
        self.sandbox.mkdir(parents=True)
        for relative, content in env.files.items():
            target = self.sandbox / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for relative, content in env.vault.items():
            target = self.vault / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._reset_reminders(env.reminders)
        if fresh_session:
            session_id = uuid.uuid4().hex
            self.core.orchestrator.session_id = session_id
            assert self.core.bus is not None
            # The turn store records into the session row that this event creates.
            await self.core.bus.publish(
                Event(name=E.SESSION_STARTED, payload={"session_id": session_id})
            )

    def _reset_reminders(self, seeded: list[tuple[str, int]]) -> None:
        assert self.core is not None and self.core.db is not None
        db = self.core.db
        db.execute("DELETE FROM reminders")
        now = datetime.now(UTC)
        for text, minutes in seeded:
            db.execute(
                "INSERT INTO reminders (id, text, due_at, created_at, source) "
                "VALUES (?, ?, ?, ?, 'eval')",
                (
                    uuid.uuid4().hex,
                    text,
                    (now + timedelta(minutes=minutes)).isoformat(),
                    now.isoformat(),
                ),
            )

    def sql_rows(self, query: str) -> list[tuple[Any, ...]]:
        assert self.core is not None and self.core.db is not None
        return [tuple(row) for row in self.core.db.fetch_all(query)]

    def render(self, text: str) -> str:
        """Fill the placeholders a prompt may use: `{web}` (fake web server)."""
        return text.replace("{web}", self.web.url)
