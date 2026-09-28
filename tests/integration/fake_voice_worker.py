"""A voice worker without audio: the real `VoiceWorker` and IPC client, fake engines.

Spawned by the core under test in place of `python -m nox.worker`. `FAKE_VOICE_MODE=ok` runs a
worker whose engines load; `missing_model` fails to load them the way a missing Piper voice does.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from nox.core.config import VoiceConfig
from nox.core.events import HealthStatus
from nox.ipc.client import IpcClient
from nox.worker.main import EXIT_STARTUP_FAILED, VoiceWorker, WorkerStartupError


class QuietPipeline:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def interrupt(self, *, reason: str) -> None:
        return None

    async def refresh_gate(self) -> None:
        return None

    async def push_to_talk(self, pressed: bool) -> None:
        return None

    async def set_muted(self, muted: bool) -> None:
        return None

    async def say(self, request: Any) -> None:
        return None

    def capture_health(self) -> tuple[HealthStatus, str]:
        return HealthStatus.AVAILABLE, "fake"


async def load(_cfg: VoiceConfig) -> tuple[Any, Any]:
    if os.environ.get("FAKE_VOICE_MODE") == "missing_model":
        raise FileNotFoundError("voice model missing: /opt/nox-test/models/piper/de_DE.onnx")
    return None, None


async def main() -> int:
    service = sys.argv[sys.argv.index("--service") + 1]
    worker: VoiceWorker
    client = IpcClient(
        os.environ["NOX_HUB_URL"],
        os.environ["NOX_WORKER_TOKEN"],
        "worker",
        f"worker:{service}",
        client_version="0.1.0",
        reconnect=True,
        backoff_initial_s=0.05,
        on_connection_change=lambda connected: worker.on_connection_change(connected),
        on_reconnect_refused=lambda error: worker.on_reconnect_refused(error),
    )
    worker = VoiceWorker(
        client=client,
        service=service,
        pipeline_factory=lambda *_args: QuietPipeline(),  # type: ignore[arg-type,return-value]
        component_loader=load,
    )
    try:
        await worker.run()
    except WorkerStartupError:
        return EXIT_STARTUP_FAILED
    return worker.exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
