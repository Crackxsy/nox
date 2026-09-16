"""NoxStateManager: owns the live `NoxState`, implements `nox.core.state.StateManager` (Data Model
L1).

Every mutation goes through `update(path, value, reason)`: the dotted path is set on a JSON dump
of the tree, the whole tree is re-validated (so nested sub-models and dict leaves are validated
uniformly), `version` is bumped, `state.changed` is published with a `StateChanged` payload, and a
checkpoint is either written immediately (mode, privacy, kill/level, task paths or an explicit
`reason` prefix) or scheduled with a debounce of `checkpoint_interval_s` (default 5 s) into a
`StateCheckpointRepository`. Checkpoint writes call the repository directly (sub-millisecond
SQLite insert, see nox.data.db).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from nox.core.events import E, Event, EventBus, StateChanged
from nox.core.logging import get_logger
from nox.core.state import NoxState
from nox.data.repos import StateCheckpointRepository

IMMEDIATE_PATH_PREFIXES: tuple[str, ...] = (
    "system.level",
    "assistant.mode",
    "assistant.layers",
    "assistant.current_task",
    "privacy.mode",
    "privacy.panic",
)
IMMEDIATE_REASON_PREFIXES: tuple[str, ...] = ("mode", "kill", "privacy", "task", "panic", "safe")


class StatePathError(KeyError):
    """Unknown dotted path in NoxState."""


def _walk(data: Any, parts: list[str], path: str) -> Any:
    node = data
    for part in parts:
        if isinstance(node, dict):
            if part not in node:
                raise StatePathError(path)
            node = node[part]
        elif hasattr(node, part) and not part.startswith("_"):
            node = getattr(node, part)
        else:
            raise StatePathError(path)
    return node


def _dynamic_dict_container(state: Any, parts: list[str], path: str) -> dict[str, Any] | None:
    """If `parts[:-1]` resolves, on the live model, to a real `dict` field (`mood_estimate`,
    `health`, `providers`, ...), return it. Those fields accept arbitrary keys by design, so
    probing one that is not present yet is not a `StatePathError` - unlike an unknown attribute
    on a pydantic sub-model, which still is."""
    if len(parts) < 2:
        return None
    try:
        container = _walk(state, parts[:-1], path)
    except StatePathError:
        return None
    return container if isinstance(container, dict) else None


class NoxStateManager:
    def __init__(
        self,
        bus: EventBus,
        repo: StateCheckpointRepository | None = None,
        *,
        checkpoint_interval_s: float = 5.0,
        state: NoxState | None = None,
    ) -> None:
        self._bus = bus
        self._repo = repo
        self._interval = checkpoint_interval_s
        self._state = state or NoxState()
        self._log = get_logger(__name__)
        self._pending: asyncio.Task[None] | None = None
        self._pending_reason = ""
        self._last_checkpoint_version = -1
        self._lock = asyncio.Lock()

    # ---- read --------------------------------------------------------------------------------

    @property
    def state(self) -> NoxState:
        return self._state

    def get(self, path: str) -> Any:
        """Return the live value at a dotted path (models for sub-trees, raw values for leaves)."""
        if not path:
            return self._state
        return _walk(self._state, path.split("."), path)

    def snapshot(self) -> dict[str, Any]:
        return self._state.model_dump(mode="json")

    # ---- write -------------------------------------------------------------------------------

    async def update(self, path: str, value: Any, *, reason: str = "") -> int:
        if not path:
            raise StatePathError("empty path")
        parts = path.split(".")
        if parts[0] in ("version", "schema_version", "updated_at"):
            raise StatePathError(f"{path} is managed by the state manager")
        async with self._lock:
            old_dump = self._state.model_dump(mode="json")
            try:
                old = _walk(old_dump, parts, path)
            except StatePathError:
                if _dynamic_dict_container(self._state, parts, path) is None:
                    raise
                old = None
            data = self._state.model_dump()
            parent = _walk(data, parts[:-1], path)
            if not isinstance(parent, dict):
                raise StatePathError(path)
            parent[parts[-1]] = value
            data["version"] = self._state.version + 1
            data["updated_at"] = datetime.now(UTC)
            try:
                new_state = NoxState.model_validate(data)
            except ValidationError as exc:
                errors = "; ".join(
                    ".".join(str(p) for p in err["loc"]) + ": " + err["msg"] for err in exc.errors()
                )
                raise ValueError(f"invalid value for {path}: {errors}") from exc
            self._state = new_state
            new = _walk(new_state.model_dump(mode="json"), parts, path)
            version = new_state.version

        payload = StateChanged(path=path, old=old, new=new, version=version).model_dump(mode="json")
        await self._bus.publish(Event(name=E.STATE_CHANGED, payload=payload, source="statemgr"))

        immediate = path.startswith(IMMEDIATE_PATH_PREFIXES) or reason.startswith(
            IMMEDIATE_REASON_PREFIXES
        )
        await self.checkpoint(immediate=immediate, reason=reason or path)
        return version

    # ---- checkpoints -------------------------------------------------------------------------

    async def checkpoint(self, *, immediate: bool = False, reason: str = "") -> None:
        """Write now (`immediate`) or coalesce writes within `checkpoint_interval_s`."""
        if self._repo is None:
            return
        if immediate:
            self._cancel_pending()
            self._write(reason or "immediate")
            return
        self._pending_reason = reason or self._pending_reason or "scheduled"
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._delayed_write(), name="nox-state-checkpoint")

    async def _delayed_write(self) -> None:
        await asyncio.sleep(self._interval)
        self._write(self._pending_reason or "scheduled")
        self._pending_reason = ""

    def _write(self, reason: str) -> None:
        if self._repo is None or self._state.version == self._last_checkpoint_version:
            return
        try:
            self._repo.save(self._state.version, reason, self._state.model_dump_json())
            self._last_checkpoint_version = self._state.version
        except Exception as exc:  # noqa: BLE001 - a failing checkpoint must not take the core down
            self._log.error("state.checkpoint_failed", error=f"{type(exc).__name__}: {exc}")

    def _cancel_pending(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = None

    async def flush(self) -> None:
        """Write any pending checkpoint now (used at shutdown)."""
        self._cancel_pending()
        self._write(self._pending_reason or "flush")
        self._pending_reason = ""

    async def restore_latest(self) -> bool:
        """Replace the live state with the newest checkpoint. False -> keep the fresh state."""
        if self._repo is None:
            return False
        row = self._repo.latest()
        if row is None:
            return False
        try:
            restored = NoxState.model_validate_json(row.state_json)
        except ValidationError as exc:
            self._log.warning("state.restore_invalid", checkpoint_id=row.id, error=str(exc)[:200])
            return False
        if restored.schema_version != NoxState().schema_version:
            self._log.warning(
                "state.restore_schema_mismatch",
                found=restored.schema_version,
                expected=NoxState().schema_version,
            )
            return False
        async with self._lock:
            self._state = restored
            self._last_checkpoint_version = restored.version
        self._log.info("state.restored", version=restored.version, checkpoint_id=row.id)
        return True

    async def close(self) -> None:
        await self.flush()
