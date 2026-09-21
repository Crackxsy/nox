"""In-memory sensor sample ring buffer.

Samples live only in memory, bounded per sensor by `sensors.history_len`. That is what
`sensors.status.read` needs for its live and recent view; a persisted long-term history would need
its own table and retention rules, and deliberately does not exist yet.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Any


class RingBuffer:
    """Fixed-capacity FIFO of plain dicts (already the shape a tool/dashboard wants back)."""

    def __init__(self, maxlen: int) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1")
        self._buf: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def add(self, sample: Mapping[str, Any]) -> None:
        self._buf.append(dict(sample))

    def latest(self) -> dict[str, Any] | None:
        return self._buf[-1] if self._buf else None

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = list(self._buf)
        return items if limit is None else items[-limit:]

    def __len__(self) -> int:
        return len(self._buf)


class SensorHistoryStore:
    """One `RingBuffer` per sensor name (`"foreground"`, `"idle"`, `"resources"`, `"game"`)."""

    def __init__(self, *, maxlen: int = 120) -> None:
        self._maxlen = maxlen
        self._buffers: dict[str, RingBuffer] = {}

    def record(self, sensor: str, sample: Mapping[str, Any]) -> None:
        self._buffers.setdefault(sensor, RingBuffer(self._maxlen)).add(sample)

    def latest(self, sensor: str) -> dict[str, Any] | None:
        buf = self._buffers.get(sensor)
        return buf.latest() if buf else None

    def recent(self, sensor: str, limit: int | None = None) -> list[dict[str, Any]]:
        buf = self._buffers.get(sensor)
        return buf.recent(limit) if buf else []

    def snapshot(self) -> dict[str, dict[str, Any] | None]:
        return {name: buf.latest() for name, buf in self._buffers.items()}
