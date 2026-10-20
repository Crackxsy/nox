"""The `voice.model.*` requests behind the dashboard's "Voice" tile.

    voice.model.status    {}  -> which model files exist, what is missing, the last run's state
    voice.model.download  {}  -> {ok, reason?, ...status}; the run continues in the background and
                                 the tile polls `status`

Clicking "download" is the consent. The core does not fetch the files itself - it may reach the
network only through the egress guard, and the model host is on none of its allow-lists - but
starts `python -m nox.worker --download-<engine> --progress-json --models-dir <root>` and reads the
JSON lines it prints (`nox.voice.model_setup.JsonProgress`). The privacy modes `offline` and
`private` refuse before that process is started, and switching to one of them while a download runs
terminates the process and removes its half-written `.part` files (`watch`). One run at a time; a
second `download` while one is running is refused, not queued.

Outcomes, each said as it is: `done` (the files are there; the voice worker loaded its engine at
start, so Nox speaks with them from its next start), `already_present`, `privacy_blocked` (refused,
or stopped mid-run by a privacy switch), and `failed` with the reason the download gave.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from nox.core.events.names import E
from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
from nox.ipc.roles import LOCAL_UI_ROLES
from nox.voice._logging import get_logger
from nox.voice.model_setup import (
    download_allowed,
    model_status,
    parse_progress_line,
)

log = get_logger(__name__)

__all__ = ["VoiceModelService", "register_voice_model_ipc", "spawn_download"]

#: How many characters of the downloader's last words survive into the outcome.
_DETAIL_MAX = 300
#: The fields of a `progress` line the tile gets to see.
_PROGRESS_KEYS = ("file", "file_done", "file_total", "done", "total", "index", "count")
#: How long a privacy abort waits for the terminated downloader before it stops waiting for it.
_ABORT_WAIT_S = 10.0


class _Stream(Protocol):
    async def readline(self) -> bytes: ...


class DownloadProcess(Protocol):
    """What the service needs of the subprocess (an `asyncio.subprocess.Process`, or a fake)."""

    pid: int
    returncode: int | None
    stdout: _Stream | None

    async def wait(self) -> int: ...
    def terminate(self) -> None: ...


SpawnFn = Callable[[Sequence[str]], Awaitable[DownloadProcess]]


async def spawn_download(argv: Sequence[str]) -> DownloadProcess:
    """Start the downloader: no console window, no stdin, stdout as a pipe, UTF-8 throughout."""
    env = dict(os.environ)
    env.pop("NOX_WORKER_TOKEN", None)  # a downloader is not a worker and never talks to the hub
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if sys.platform == "win32" else 0
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        creationflags=flags,
    )
    return process  # type: ignore[return-value]


class VoiceModelService:
    """Holds the one download the dashboard can see, and starts new ones."""

    def __init__(
        self,
        *,
        engine: Callable[[], str],
        models_root: Callable[[], Path],
        privacy_mode: Callable[[], str],
        spawn: SpawnFn = spawn_download,
        python: str = sys.executable,
        on_spawn: Callable[[int], None] = lambda _pid: None,
    ) -> None:
        self._engine = engine
        self._root = models_root
        self._privacy_mode = privacy_mode
        self._spawn = spawn
        self._python = python
        self._on_spawn = on_spawn
        self._task: asyncio.Task[None] | None = None
        self._process: DownloadProcess | None = None
        self._directory: Path | None = None
        #: The privacy mode that stopped the running download, once one has.
        self._abort_reason = ""
        self._unsubs: list[Callable[[], None]] = []
        self.outcome: str | None = None
        self.detail = ""
        self.progress: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        mode = self._privacy_mode()
        return {
            **model_status(self._engine(), self._root()).to_payload(),
            "running": self.running,
            "outcome": self.outcome,
            "detail": self.detail,
            "progress": self.progress,
            "privacy_mode": mode,
            "download_blocked": not download_allowed(mode),
            # The worker loads its engine once, at start; new files take effect with the next one.
            "restart_required": self.outcome == "done",
        }

    def download(self) -> dict[str, Any]:
        if self.running:
            return {"ok": False, "reason": "running", **self.status()}
        status = model_status(self._engine(), self._root())
        mode = self._privacy_mode()
        self.progress = None
        self.detail = ""
        if status.present:
            self.outcome = "already_present"
            return {"ok": True, **self.status()}
        if not download_allowed(mode):
            # Refused before anything is started: not even a process that could reach the network.
            self.outcome = "privacy_blocked"
            self.detail = mode
            log.info("voice.model_download_refused", privacy_mode=mode)
            return {"ok": False, "reason": "privacy_blocked", **self.status()}
        self.outcome = None
        self._abort_reason = ""
        self._directory = status.directory
        argv = [
            self._python,
            "-m",
            "nox.worker",
            f"--download-{status.engine}",
            "--progress-json",
            "--models-dir",
            str(self._root()),
        ]
        self._task = asyncio.get_running_loop().create_task(
            self._run(argv, status.engine), name="nox-voice-model-download"
        )
        log.info("voice.model_download_started", engine=status.engine)
        return {"ok": True, **self.status()}

    async def _run(self, argv: list[str], engine: str) -> None:
        last_log = ""
        code: int | None = None
        try:
            process = await self._spawn(argv)
            self._process = process
            self._on_spawn(process.pid)
            if self._abort_reason:  # the privacy mode changed while the process was starting
                self._terminate(process)
            if process.stdout is not None:
                while raw := await process.stdout.readline():
                    message = parse_progress_line(raw.decode("utf-8", errors="replace"))
                    if message is None:
                        continue
                    kind = message["type"]
                    if kind == "progress":
                        self.progress = {key: message.get(key) for key in _PROGRESS_KEYS}
                    elif kind == "log":
                        text = str(message.get("text", ""))
                        if text.startswith("failed"):
                            last_log = text
                    elif kind == "result":
                        code = int(message.get("code", 1))
            exit_code = await process.wait()
        except asyncio.CancelledError:
            if self._abort_reason:
                self._finish_privacy_abort(engine)
            else:
                self.outcome = "failed"
                self.detail = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - the tile shows the reason instead of a traceback
            self.outcome = "failed"
            self.detail = f"{type(exc).__name__}: {exc}"[:_DETAIL_MAX]
            log.warning("voice.model_download_failed", engine=engine, error=self.detail)
            return
        finally:
            self._process = None
        if self._abort_reason:
            self._finish_privacy_abort(engine)
            return
        code = exit_code if code is None else code
        after = model_status(self._engine(), self._root())
        if code == 0 and after.present:
            self.outcome = "done"
            self.detail = ""
            log.info("voice.model_downloaded", engine=engine)
            return
        self.outcome = "failed"
        if last_log:
            self.detail = last_log.removeprefix("failed").strip()[:_DETAIL_MAX]
        elif code == 0:
            self.detail = f"still missing: {', '.join(after.missing)}"[:_DETAIL_MAX]
        else:
            self.detail = f"exit code {code}"
        log.warning("voice.model_download_failed", engine=engine, error=self.detail)

    # ---- privacy ---------------------------------------------------------------------------------

    def watch(self, bus: Any) -> None:
        """Stop a running download as soon as the privacy mode no longer allows one."""
        self._unsubs.append(bus.subscribe(E.PRIVACY_MODE_CHANGED, self._on_privacy_mode))

    async def _on_privacy_mode(self, event: Any) -> None:
        payload = getattr(event, "payload", None) or {}
        mode = str(payload.get("current") or self._privacy_mode())
        if not download_allowed(mode):
            await self.abort_for_privacy(mode)

    async def abort_for_privacy(self, mode: str) -> bool:
        """Terminate the running download (if any) because `mode` allows none; True when one was
        stopped. Its half-written `.part` files are removed and the outcome is `privacy_blocked`."""
        task = self._task
        if task is None or task.done():
            return False
        self._abort_reason = mode
        process = self._process
        if process is not None:
            self._terminate(process)
        log.info("voice.model_download_aborted", privacy_mode=mode)
        _done, pending = await asyncio.wait({task}, timeout=_ABORT_WAIT_S)
        if pending:  # the process did not end after `terminate`: stop reading it regardless
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    @staticmethod
    def _terminate(process: DownloadProcess) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    def _finish_privacy_abort(self, engine: str) -> None:
        self.outcome = "privacy_blocked"
        self.detail = self._abort_reason
        self.progress = None
        directory = self._directory
        if directory is not None and directory.is_dir():
            for part in directory.rglob("*.part"):
                try:
                    part.unlink()
                except OSError as exc:
                    log.warning("voice.model_part_not_removed", file=part.name, error=str(exc))
        log.info("voice.model_download_stopped", engine=engine, privacy_mode=self._abort_reason)

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        process = self._process
        if process is not None:
            self._terminate(process)
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


def register_voice_model_ipc(registry: RequestRegistry, service: VoiceModelService) -> None:
    async def status(_ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        return service.status()

    async def download(ctx: RequestContext, _payload: EmptyPayload) -> dict[str, Any]:
        log.info("voice.model_download_requested", by=ctx.role)
        return service.download()

    # Downloading a few hundred MB onto this machine is administration: the local UI only.
    registry.register("voice.model.status", EmptyPayload, status, roles=LOCAL_UI_ROLES)
    registry.register("voice.model.download", EmptyPayload, download, roles=LOCAL_UI_ROLES)
