"""The three types every other event module builds on: the two enumerations and `Event`.

Split out so a payload module can import what it needs without reaching for the whole
catalogue, and so that reading `Event` does not mean scrolling past ninety payload
models first."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Severity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class HealthStatus(StrEnum):
    AVAILABLE = "available"
    LIMITED = "limited"
    UNAVAILABLE = "unavailable"


class Event(BaseModel):
    """In-process event.

    Over IPC it is wrapped in an Envelope(kind=event, name=name, payload=payload).
    """

    model_config = ConfigDict(frozen=True)
    name: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    corr: str | None = None
    source: str = "core"
