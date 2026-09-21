"""The kill switch and panic mode.

`engage()` flips the safe-mode flag first, because the permission engine and the privacy service
read it on every decision. It then audits the engagement, emits `security.kill_switch`, and runs
every registered stop hook in parallel with a per-hook timeout. The audit entry is written before
the hooks run, so a hook that fails cannot cost us the record that the kill happened; the hook
results are appended in a second entry. `engage()` never raises.

`panic()` is `engage()` plus privacy offline, the panic flag and `security.panic`, which hides the
pet.

`resume()` needs `pin_ok=True` after a security-path kill - tamper, a broken audit chain, panic.
After a user-initiated kill (tray, hotkey, UI, dashboard, voice) the explicit request is enough.
Both outcomes are audited with the origin and whether it was a security-path kill.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from nox.core.events import E, EventBus
from nox.core.events import KillSwitch as KillSwitchPayload
from nox.core.state import PrivacyMode
from nox.security._events import publish
from nox.security._logging import get_logger
from nox.security.audit_sink import SafeAuditLog
from nox.security.constants import SECURITY_PATH_ORIGINS
from nox.security.model import AuditLog
from nox.security.privacy import PrivacyService

log = get_logger(__name__)

StopHook = Callable[[], Awaitable[None]]
Clock = Callable[[], datetime]

__all__ = [
    "SECURITY_PATH_ORIGINS",
    "KillReport",
    "KillSwitchService",
    "PanicModeService",
]


class KillReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    engaged: bool
    already_engaged: bool = False
    origin: str = ""
    reason: str = ""
    security_path: bool = False  # True -> resuming needs the PIN
    hooks: dict[str, str] = Field(default_factory=dict)  # name -> ok | timeout | error:<ExcType>
    #: Empty when the engagement completed; otherwise what went wrong while publishing or running
    #: the stop hooks. The switch is engaged either way - this says the report is incomplete.
    partial_failure: str = ""


class KillSwitchService:
    """Implements `nox.security.model.KillSwitch`; `PanicModeService` adapts it to `PanicMode`."""

    def __init__(
        self,
        *,
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
        privacy: PrivacyService | None = None,
        hook_timeout_s: float = 2.0,
        clock: Clock | None = None,
    ) -> None:
        self._bus = bus
        self._audit = SafeAuditLog(audit)
        self._privacy = privacy
        self._hook_timeout = hook_timeout_s
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._hooks: dict[str, StopHook] = {}
        self._engaged = False
        self._engaged_at: datetime | None = None
        self._origin = ""
        self._reason = ""
        self._security_path = False

    def register_stop_hook(self, name: str, hook: StopHook) -> Callable[[], None]:
        self._hooks[name] = hook

        def _unregister() -> None:
            self._hooks.pop(name, None)

        return _unregister

    # ---- KillSwitch protocol ---------------------------------------------------------------------

    def is_engaged(self) -> bool:
        return self._engaged

    async def trigger(self, *, by: str, reason: str = "") -> None:
        await self.engage(by, reason)

    @property
    def engaged_at(self) -> datetime | None:
        return self._engaged_at

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def security_path(self) -> bool:
        """Whether the engaged kill came from the security path (resume then needs the PIN)."""
        return self._security_path

    async def engage(
        self, origin: str, reason: str = "", *, security_path: bool | None = None
    ) -> KillReport:
        already = self._engaged
        by_security_path = (
            origin in SECURITY_PATH_ORIGINS if security_path is None else security_path
        )
        # Sticky while engaged: a user kill on top of a security-path kill must not clear the flag.
        self._security_path = by_security_path or (already and self._security_path)
        self._engaged = True
        self._engaged_at = self._clock()
        self._origin = origin
        self._reason = reason
        report = KillReport(
            engaged=True,
            already_engaged=already,
            origin=origin,
            reason=reason,
            security_path=self._security_path,
        )
        failure = ""
        # The event goes out first: the permission engine, the UI and the workers all react to it,
        # and every millisecond here is a millisecond in which something may still act.
        try:
            await publish(
                self._bus, E.SECURITY_KILL_SWITCH, KillSwitchPayload(by=origin, reason=reason)
            )
        except Exception as exc:  # noqa: BLE001 - the kill switch must never raise
            failure = f"{type(exc).__name__}: {exc}"
            log.exception("security.kill_switch_publish_error")
        # Audited before the hooks run, and unconditionally: a kill that happened must be in the
        # log even when a stop hook then fails. Previously the audit sat after the hooks inside
        # the same `try`, so a failure anywhere above it cost the record entirely.
        self._audit.append(
            actor=origin,
            action="kill_switch.engage",
            details={
                "reason": reason,
                "already_engaged": str(already).lower(),
                "security_path": str(self._security_path).lower(),
                "publish_error": failure,
            },
        )
        hooks: dict[str, str] = {}
        try:
            hooks = await self._run_hooks()
            log.critical("security.kill_switch_engaged", by=origin, reason=reason, hooks=hooks)
        except Exception as exc:  # noqa: BLE001 - the kill switch must never raise
            failure = f"{failure}; {type(exc).__name__}: {exc}".lstrip("; ")
            log.exception("security.kill_switch_hooks_error")
        self._audit.append(
            actor=origin,
            action="kill_switch.hooks",
            decision="allow" if not failure else "deny",
            result="ok" if not failure else "failed",
            details={**{f"hook.{k}": v for k, v in hooks.items()}, "error": failure},
        )
        return report.model_copy(update={"hooks": hooks, "partial_failure": failure})

    async def _run_hooks(self) -> dict[str, str]:
        names = list(self._hooks)
        results = await asyncio.gather(
            *(self._run_hook(n, self._hooks[n]) for n in names), return_exceptions=True
        )
        out: dict[str, str] = {}
        for name, result in zip(names, results, strict=True):
            out[name] = result if isinstance(result, str) else f"error:{type(result).__name__}"
        return out

    async def _run_hook(self, name: str, hook: StopHook) -> str:
        try:
            await asyncio.wait_for(hook(), self._hook_timeout)
        except TimeoutError:
            log.warning("security.stop_hook_timeout", hook=name, timeout_s=self._hook_timeout)
            return "timeout"
        except Exception as exc:  # noqa: BLE001 - one failing hook must not stop the others
            log.warning("security.stop_hook_failed", hook=name, error=type(exc).__name__)
            return f"error:{type(exc).__name__}"
        return "ok"

    async def resume(self, *, pin_ok: bool, by: str = "user") -> bool:
        """Leave safe mode.

        The PIN is required only after a security-path kill; otherwise the explicit request is the
        authorisation. Both outcomes are audited with the origin and the security-path flag.
        """
        if not self._engaged:
            return True
        security_path = self._security_path
        origin = self._origin
        if security_path and not pin_ok:
            self._audit.append(
                actor=by,
                action="kill_switch.resume",
                target="",
                decision="deny",
                result="denied",
                details={
                    "reason": "pin not verified after a security-path kill",
                    "security_path": "true",
                    "origin": origin,
                },
            )
            log.warning("security.resume_denied", by=by, origin=origin)
            return False
        self._engaged = False
        self._engaged_at = None
        self._origin = ""
        self._reason = ""
        self._security_path = False
        try:
            if self._privacy is not None:
                await self._privacy.set_panic(False, by=by)
        except Exception:  # noqa: BLE001
            log.exception("security.resume_privacy_error")
        self._audit.append(
            actor=by,
            action="kill_switch.resume",
            target="",
            details={"security_path": str(security_path).lower(), "origin": origin},
        )
        log.info("security.kill_switch_resumed", by=by, origin=origin, security_path=security_path)
        return True

    async def panic(self, *, by: str = "user", reason: str = "panic") -> KillReport:
        report = await self.engage(by, reason, security_path=True)
        try:
            if self._privacy is not None:
                await self._privacy.set_mode(PrivacyMode.OFFLINE, by="system")
                await self._privacy.set_panic(True, by=by)
            await publish(
                self._bus, E.SECURITY_PANIC, {"by": by, "reason": reason, "hide_pet": True}
            )
            self._audit.append(
                actor=by, action="panic.engage", target="", details={"reason": reason}
            )
        except Exception:  # noqa: BLE001 - panic must never raise
            log.exception("security.panic_error")
        return report


class PanicModeService:
    """Implements `nox.security.model.PanicMode` on top of `KillSwitchService`."""

    def __init__(self, killswitch: KillSwitchService) -> None:
        self._killswitch = killswitch

    async def engage(self, *, by: str) -> None:
        await self._killswitch.panic(by=by)

    async def release(self, *, by: str, pin_ok: bool) -> None:
        await self._killswitch.resume(pin_ok=pin_ok, by=by)
