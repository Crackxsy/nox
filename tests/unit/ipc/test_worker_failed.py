"""`worker.failed`: a worker's own account of its failure reaches health, without any path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.ipc.handlers.core import CoreHandlers, WorkerFailed, without_paths
from nox.ipc.protocol import Envelope, Kind, Source


class RecordingWorkers:
    def __init__(self) -> None:
        self.reports: list[tuple[str, str]] = []

    def report_failure(self, service: str, reason: str) -> None:
        self.reports.append((service, reason))


def _context(client_id: str) -> RequestContext:
    env = Envelope(kind=Kind.REQUEST, name="worker.failed", src=Source(role="worker", id=client_id))
    return RequestContext(client_id=client_id, role="worker", request=env)


async def test_the_reason_is_kept_without_the_path() -> None:
    workers = RecordingWorkers()
    handlers = CoreHandlers(cast("Any", SimpleNamespace(workers=workers)))

    await handlers.worker_failed(
        _context("worker:voice"),
        WorkerFailed(
            service="voice",
            reason="FileNotFoundError: voice model missing: /home/someone/nox/models/de.onnx",
        ),
    )

    assert workers.reports == [("voice", "FileNotFoundError: voice model missing: de.onnx")]


async def test_a_worker_cannot_report_for_another_service() -> None:
    workers = RecordingWorkers()
    handlers = CoreHandlers(cast("Any", SimpleNamespace(workers=workers)))

    with pytest.raises(IpcError) as denied:
        await handlers.worker_failed(
            _context("worker:stt"), WorkerFailed(service="voice", reason="fake")
        )

    assert denied.value.code == ERR_PERMISSION
    assert workers.reports == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (r"cannot open C:\Users\someone\AppData\Nox\models\kokoro.onnx", "cannot open kokoro.onnx"),
        ("no such file: /var/lib/nox/", "no such file: <path>"),
        ("device 3 at ratio 1/2 failed", "device 3 at ratio 1/2 failed"),
    ],
)
def test_paths_are_reduced_to_their_file_name(text: str, expected: str) -> None:
    assert without_paths(text) == expected
