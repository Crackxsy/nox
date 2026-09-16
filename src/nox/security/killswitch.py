"""Kill switch and panic mode (Security Model §6, model.py KillSwitch/PanicMode, PRD P11).

`engage()` flips the safe-mode flag first (the permission engine and privacy service read it), emits
`security.kill_switch` before anything else, then runs every registered stop hook in parallel with a
per-hook timeout, and audits. It never raises. `panic()` = engage + privacy OFFLINE + panic flag +
`security.panic` (hide pet).

`resume()` follows OP-6 D: a kill engaged by the security path (tamper, audit-chain break, panic,
supervisor tamper) needs `pin_ok=True`; a user-initiated kill (tray, hotkey, UI, dashboard, voice,
supervisor) resumes on an explicit action alone. Every resume is audited with `origin` and
`security_path`.
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
from nox.security.model import AuditLog
from nox.security.privacy import PrivacyService

log = get_logger(__name__)

StopHook = Callable[[], Awaitable[None]]
Clock = Callable[[], datetime]

#: Origins that make the kill a security-path event; resuming from one requires the PIN (OP-6 D).
#: Everything else (`ui`, `shell`, `dashboard`, `hotkey`, `tray`, `voice`, `supervisor`) is a
#: user-initiated kill and resumes on an explicit, audited action.
SECURITY_PATH_ORIGINS: frozenset[str] = frozenset({"tamper", "audit", "panic", "supervisor-tamper"})


class KillReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    engaged: bool
    already_engaged: bool = False
    origin: str = ""
    reason: str = ""
    security_path: bool = False  # True -> resume needs the PIN (OP-6 D)
    hooks: dict[str, str] = Field(default_factory=dict)  # name -> ok | timeout | error:<ExcType>


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
        self._audit = audit
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
        try:
            await publish(
                self._bus, E.SECURITY_KILL_SWITCH, KillSwitchPayload(by=origin, reason=reason)
            )
            hooks = await self._run_hooks()
            report = report.model_copy(update={"hooks": hooks})
            self._audit_safe(
                actor=origin,
                action="kill_switch.engage",
                target="",
                details={
                    "reason": reason,
                    "already_engaged": str(already).lower(),
                    "security_path": str(self._security_path).lower(),
                    **{f"hook.{k}": v for k, v in hooks.items()},
                },
            )
            log.critical("security.kill_switch_engaged", by=origin, reason=reason, hooks=hooks)
        except Exception:  # noqa: BLE001 - the kill switch must never raise
            log.exception("security.kill_switch_engage_error")
        return report

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
        """OP-6 D: the PIN is required only after a security-path kill; otherwise the explicit
        request counts. Both outcomes are audited with `origin` and `security_path`."""
        if not self._engaged:
            return True
        security_path = self._security_path
        origin = self._origin
        if security_path and not pin_ok:
            self._audit_safe(
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
        self._audit_safe(
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
            self._audit_safe(actor=by, action="panic.engage", target="", details={"reason": reason})
        except Exception:  # noqa: BLE001 - panic must never raise
            log.exception("security.panic_error")
        return report

    def _audit_safe(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        decision: str = "allow",
        result: str = "ok",
        details: dict[str, str] | None = None,
    ) -> None:
        if self._audit is None:
            return
        try:
            self._audit.append(
                actor=actor,
                tool="security",
                action=action,
                target=target,
                decision=decision,
                result=result,
                details=details,
            )
        except Exception:  # noqa: BLE001
            log.exception("security.audit_write_failed", action=action)


class PanicModeService:
    """Implements `nox.security.model.PanicMode` on top of `KillSwitchService`."""

    def __init__(self, killswitch: KillSwitchService) -> None:
        self._killswitch = killswitch

    async def engage(self, *, by: str) -> None:
        await self._killswitch.panic(by=by)

    async def release(self, *, by: str, pin_ok: bool) -> None:
        await self._killswitch.resume(pin_ok=pin_ok, by=by)
