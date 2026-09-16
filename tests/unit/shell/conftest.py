"""Shell test fixtures: offscreen Qt, a fake IPC bridge, a fake supervisor."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp() -> Iterator[Any]:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class FakeBridge:
    """Implements the IpcClientThread interface; records calls, lets tests push events."""

    def __init__(self, url: str, token: str, role: str, client_id: str) -> None:
        self.url, self.token, self.role, self.client_id = url, token, role, client_id
        self.started = False
        self.stopped = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []
        self.fail_calls = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def call(self, name: str, payload: dict[str, Any]) -> Future[Any]:
        self.calls.append((name, payload))
        fut: Future[Any] = Future()
        if self.fail_calls:
            fut.set_exception(ConnectionError("down"))
        else:
            fut.set_result({"ok": True})
        return fut

    def on_event(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._callbacks.append(callback)

    def emit(self, name: str, payload: dict[str, Any]) -> None:
        env = {
            "v": 1,
            "id": "x",
            "kind": "event",
            "name": name,
            "src": {"role": "core", "id": "core"},
            "payload": payload,
        }
        for cb in self._callbacks:
            cb(env)


@pytest.fixture
def fake_bridge_factory() -> tuple[Callable[..., FakeBridge], list[FakeBridge]]:
    created: list[FakeBridge] = []

    def factory(url: str, token: str, role: str, client_id: str) -> FakeBridge:
        b = FakeBridge(url, token, role, client_id)
        created.append(b)
        return b

    return factory, created
