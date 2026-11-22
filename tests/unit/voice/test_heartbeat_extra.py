"""A heartbeat report that cannot be built costs the beat its extras, never the beat."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nox.worker import heartbeat
from nox.worker.heartbeat import heartbeat_loop
from tests.unit.voice.conftest import FakeIpcClient


@pytest.fixture
def beats(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Three beats, then the loop is told to stop; no waiting between them."""
    seen: list[int] = []

    async def no_wait(_stop: asyncio.Event, _timeout: float) -> bool:
        seen.append(1)
        return len(seen) >= 3

    monkeypatch.setattr(heartbeat, "wait_or_stop", no_wait)
    return seen


async def test_a_report_that_raises_does_not_stop_the_heartbeat(beats: list[int]) -> None:
    client = FakeIpcClient()
    calls = {"n": 0}

    def extra() -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AttributeError("the pipeline has no such report")
        return {"capture": "available"}

    await heartbeat_loop(
        client, asyncio.Event(), status=lambda: "running", interval_s=1.0, extra=extra
    )

    sent = [p for n, p in client.requests if n == "worker.heartbeat"]
    assert [p["status"] for p in sent] == ["running"] * 3  # none of the beats was lost
    assert "capture" not in sent[0] and sent[1]["capture"] == "available"


async def test_the_status_is_never_overwritten_by_the_report(beats: list[int]) -> None:
    client = FakeIpcClient()

    await heartbeat_loop(
        client,
        asyncio.Event(),
        status=lambda: "running",
        interval_s=1.0,
        extra=lambda: {"status": "stopping", "capture": "available"},
    )

    assert {p["status"] for n, p in client.requests if n == "worker.heartbeat"} == {"running"}
