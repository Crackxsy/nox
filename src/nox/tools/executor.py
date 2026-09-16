"""ToolExecutor: the permission-checked execution pipeline (Tool Model "Execution pipeline").

Steps, in order: registry lookup (unknown -> refused) -> input validation against the tool's
`input_model` (invalid -> tool error, never executed) -> `PermissionRequest` built from the tool
name/target -> `PermissionEngine.check()` (deny -> tool error; confirm -> `request_confirmation()`
and wait, 60 s timeout = deny; allow -> run) -> execution with a timeout and cancellation on
`security.kill_switch` -> every outcome is audited (actor/tool/action/target/decision/duration,
never the tool's input or output - those may carry private content).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from nox.core.events import E, EventBus
from nox.core.logging import get_logger
from nox.security.model import AuditLog, Decision, KillSwitch, PermissionRequest, PermissionResult
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

DEFAULT_TIMEOUT_S = 30.0

# Tool-error codes returned to the model/orchestrator (never a raised exception).
ERR_UNKNOWN_TOOL = "tool.unknown"
ERR_INVALID_INPUT = "tool.invalid_input"
ERR_PERMISSION_DENIED = "permission.denied"
ERR_TIMEOUT = "tool.timeout"
ERR_CANCELLED = "tool.cancelled"
ERR_FAILED = "tool.error"


class ConfirmingPermissionEngine(Protocol):
    """The subset of `DefaultPermissionEngine` the executor needs: `check()` plus the confirmation
    round-trip (`request_confirmation`/`await_confirmation`). Kept local rather than widening the
    shared `nox.security.model.PermissionEngine` Protocol."""

    def check(self, request: PermissionRequest) -> PermissionResult: ...
    def request_confirmation(self, request: PermissionRequest) -> str: ...
    async def await_confirmation(
        self, grant_id: str, *, timeout: float | None = None
    ) -> PermissionResult: ...


class ToolResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None
    decision: str = Decision.DENY.value
    duration_ms: float = 0.0


class ToolTimeoutError(Exception):
    """The handler did not finish within the executor's timeout."""


class ToolCancelledError(Exception):
    """The kill switch engaged while the handler was running."""


def split_tool_action(name: str) -> tuple[str, str]:
    """Dotted tool name -> (tool, action) the way `PermissionRequest` expects it: `obs.scene.switch`
    -> ("obs", "scene.switch"); `time.now` -> ("time", "now"); a bare name has no action."""
    tool, _, action = name.partition(".")
    return tool, action


class ToolExecutor:
    """Implements `nox.tools.executor`'s pipeline; see module docstring."""

    def __init__(
        self,
        registry: ToolRegistry,
        permission_engine: ConfirmingPermissionEngine,
        audit: AuditLog,
        bus: EventBus | None,
        killswitch: KillSwitch,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self._permission_engine = permission_engine
        self._audit = audit
        self._bus = bus
        self._killswitch = killswitch
        self._timeout_s = timeout_s
        self._clock = clock

    async def call(
        self,
        agent: str,
        name: str,
        arguments: dict[str, Any],
        *,
        mode: str,
        task_id: str | None = None,
        origin: str = "chat",
    ) -> ToolResult:
        started = self._clock()
        tool, action = split_tool_action(name)

        spec = self._registry.get(name)
        if spec is None:
            log.info("tools.call_refused", tool=name, reason="unknown")
            return self._finish(
                agent=agent,
                tool=tool,
                action=action,
                target="",
                task_id=task_id,
                decision=Decision.DENY,
                started=started,
                result="refused",
                ok=False,
                error=ERR_UNKNOWN_TOOL,
            )

        try:
            validated = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            log.info("tools.call_invalid_input", tool=name, errors=exc.error_count())
            return self._finish(
                agent=agent,
                tool=tool,
                action=action,
                target="",
                task_id=task_id,
                decision=Decision.DENY,
                started=started,
                result="validation_failed",
                ok=False,
                error=ERR_INVALID_INPUT,
            )

        payload = validated.model_dump(mode="json")
        target = spec.targets(payload) if spec.targets is not None else ""
        request = PermissionRequest(
            agent=agent,
            tool=tool,
            action=action,
            mode=mode,
            risk=spec.risk,
            target=target,
            task_id=task_id,
            origin=origin,
        )

        result = self._permission_engine.check(request)
        decision = result.decision
        if decision is Decision.CONFIRM:
            grant_id = self._permission_engine.request_confirmation(request)
            confirmed = await self._permission_engine.await_confirmation(grant_id)
            decision = confirmed.decision

        if decision is not Decision.ALLOW:
            return self._finish(
                agent=agent,
                tool=tool,
                action=action,
                target=target,
                task_id=task_id,
                decision=decision,
                started=started,
                result="denied",
                ok=False,
                error=ERR_PERMISSION_DENIED,
            )

        if self._killswitch.is_engaged():
            log.warning("tools.call_blocked_kill_switch", tool=name)
            return self._finish(
                agent=agent,
                tool=tool,
                action=action,
                target=target,
                task_id=task_id,
                decision=Decision.DENY,
                started=started,
                result="denied",
                ok=False,
                error=ERR_PERMISSION_DENIED,
            )

        try:
            data = await self._run(spec, payload)
        except ToolCancelledError:
            return self._finish(
                agent=agent,
                tool=tool,
                action=action,
                target=target,
                task_id=task_id,
                decision=Decision.ALLOW,
                started=started,
                result="aborted",
                ok=False,
                error=ERR_CANCELLED,
            )
        except ToolTimeoutError:
            return self._finish(
                agent=agent,
                tool=tool,
                action=action,
                target=target,
                task_id=task_id,
                decision=Decision.ALLOW,
                started=started,
                result="failed",
                ok=False,
                error=ERR_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - a failing handler is a tool error, not a crash
            log.error("tools.call_failed", tool=name, error=f"{type(exc).__name__}: {exc}")
            return self._finish(
                agent=agent,
                tool=tool,
                action=action,
                target=target,
                task_id=task_id,
                decision=Decision.ALLOW,
                started=started,
                result="failed",
                ok=False,
                error=ERR_FAILED,
            )

        return self._finish(
            agent=agent,
            tool=tool,
            action=action,
            target=target,
            task_id=task_id,
            decision=Decision.ALLOW,
            started=started,
            result="ok",
            ok=True,
            data=data,
        )

    # ---- execution with timeout + kill-switch cancellation ---------------------------------------

    async def _run(self, spec: ToolSpec, payload: dict[str, Any]) -> dict[str, Any]:
        handler_task: asyncio.Task[dict[str, Any]] = asyncio.ensure_future(spec.handler(payload))
        kill_task: asyncio.Task[Any] | None = None
        if self._bus is not None:
            kill_task = asyncio.ensure_future(self._bus.wait_for(E.SECURITY_KILL_SWITCH))
        waitables: list[asyncio.Task[Any]] = [handler_task]
        if kill_task is not None:
            waitables.append(kill_task)
        try:
            done, _pending = await asyncio.wait(
                waitables, timeout=self._timeout_s, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            handler_task.cancel()
            if kill_task is not None:
                kill_task.cancel()
            raise

        if handler_task in done:
            if kill_task is not None and not kill_task.done():
                kill_task.cancel()
            return handler_task.result()

        handler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await handler_task
        if kill_task is not None and kill_task in done:
            raise ToolCancelledError("kill switch engaged")
        if kill_task is not None:
            kill_task.cancel()
        raise ToolTimeoutError(f"tool timed out after {self._timeout_s:g}s")

    # ---- audit -------------------------------------------------------------------------------

    def _finish(
        self,
        *,
        agent: str,
        tool: str,
        action: str,
        target: str,
        task_id: str | None,
        decision: Decision,
        started: float,
        result: str,
        ok: bool,
        error: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> ToolResult:
        duration_ms = (self._clock() - started) * 1000
        self._audit.append(
            actor=agent,
            tool=tool,
            action=action,
            target=target,
            decision=decision.value,
            result=result,
            task_id=task_id,
            details={"duration_ms": f"{duration_ms:.1f}"},
        )
        return ToolResult(
            ok=ok, data=data, error=error, decision=decision.value, duration_ms=duration_ms
        )
