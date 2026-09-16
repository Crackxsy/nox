"""Pairing service (ST-17-01, Spec v0.8 §3.1/§5.2): a one-time 8-character code shown locally on
the dashboard or shell, redeemed once from the phone within five minutes, bound to the transport's
verified sender identity.

What the code is *not*: it is never sent over the transport by Nox, never logged, never audited and
never stored - only `sha256(salt + code)` reaches the database (`RemoteRepository`). Redemption is
atomic (`mark_pairing_redeemed`'s `redeemed_at IS NULL` guard), so two racing redemptions of the
same code produce exactly one device.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger
from nox.remote.models import DeviceRow, PairingResult, PairingStart
from nox.remote.repo import RemoteRepository
from nox.security.model import AuditLog

log = get_logger(__name__)

#: Crockford-style alphabet: no 0/O/1/I/L/U, so a code read off a screen cannot be mistyped into a
#: different valid code. 32 symbols x 8 characters = 40 bits, single use, 5 minutes.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8


def generate_code(length: int = CODE_LENGTH) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


class PairingService:
    def __init__(
        self,
        repo: RemoteRepository,
        *,
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
        ttl_s: float = 300.0,
        max_devices: int = 3,
        channel: str = "telegram",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._bus = bus
        self._audit = audit
        self._ttl_s = ttl_s
        self._max_devices = max_devices
        self._channel = channel
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- local side (dashboard/shell over IPC) -------------------------------------------------

    async def start(self, *, device_name: str = "") -> PairingStart:
        now = self._clock()
        self._repo.purge_expired_pairings(now)
        expires_at = now + timedelta(seconds=self._ttl_s)
        code = generate_code()
        pairing_id = self._repo.create_pairing(
            code,
            channel=self._channel,
            device_name=device_name or "phone",
            created_at=now,
            expires_at=expires_at,
        )
        self._append_audit("pair.start", target=pairing_id, decision="allow", result="ok")
        await self._publish(
            E.REMOTE_PAIRING_STARTED,
            {"pairing_id": pairing_id, "expires_at": expires_at.isoformat()},
        )
        # The code deliberately does not appear in this log line.
        log.info("remote.pairing_started", pairing_id=pairing_id, ttl_s=self._ttl_s)
        return PairingStart(
            pairing_id=pairing_id, code=code, expires_at=expires_at, ttl_s=self._ttl_s
        )

    async def revoke(self, device_id: str, *, reason: str = "dashboard") -> bool:
        device = self._repo.get_device(device_id)
        if device is None or device.revoked:
            return False
        ok = self._repo.revoke_device(device_id, reason=reason, when=self._clock())
        if ok:
            self._append_audit("unpair", target=device_id, decision="allow", result="ok")
            await self._publish(E.REMOTE_REVOKED, {"device_id": device_id, "reason": reason})
            log.info("remote.device_revoked", device_id=device_id, reason=reason)
        return ok

    def devices(self) -> list[DeviceRow]:
        return self._repo.list_devices()

    # -- phone side ----------------------------------------------------------------------------

    async def redeem(self, code: str, *, sender_id: str) -> PairingResult:
        """Redeem a one-time code for `sender_id`. Every rejection path is audited and creates no
        device row (ST-17-01)."""
        now = self._clock()
        existing = self._repo.find_device_for_sender(self._channel, sender_id)
        if existing is not None:
            return self._reject("already_paired", device_id=existing.id)

        row = self._repo.find_pairing(code.strip().upper(), channel=self._channel)
        if row is None:
            return self._reject("unknown_code")
        if row["redeemed_at"] is not None:
            return self._reject("used")
        if datetime.fromisoformat(str(row["expires_at"])) <= now:
            return self._reject("expired")
        active = [d for d in self._repo.list_devices(include_revoked=False)]
        if len(active) >= self._max_devices:
            return self._reject("max_devices")

        device = self._repo.create_device(
            name=str(row["device_name"]) or "phone",
            channel=self._channel,
            sender_id=sender_id,
            pairing_code_hash=str(row["code_hash"]),
            paired_at=now,
        )
        if not self._repo.mark_pairing_redeemed(str(row["id"]), device.id, now):
            # Lost the race: another redemption claimed this code first. Undo our row so exactly
            # one device exists, and report it as a used code.
            self._repo.revoke_device(device.id, reason="pairing_race", when=now)
            return self._reject("used")

        self._append_audit("pair.redeem", target=device.id, decision="allow", result="ok")
        await self._publish(
            E.REMOTE_PAIRED,
            {"device_id": device.id, "name": device.name, "channel": self._channel},
        )
        log.info("remote.device_paired", device_id=device.id)
        return PairingResult(ok=True, device_id=device.id, name=device.name)

    # -- helpers -------------------------------------------------------------------------------

    def _reject(self, reason: str, *, device_id: str = "") -> PairingResult:
        self._append_audit("pair.redeem", target=device_id, decision="deny", result=reason)
        log.warning("remote.pairing_rejected", reason=reason)
        return PairingResult(ok=False, reason=reason, device_id=device_id)

    def _append_audit(self, action: str, *, target: str, decision: str, result: str) -> None:
        if self._audit is None:
            return
        self._audit.append(
            actor="remote",
            tool="remote",
            action=action,
            target=target,
            decision=decision,
            result=result,
            details={"channel": self._channel},
        )

    async def _publish(self, name: str, payload: dict[str, object]) -> None:
        if self._bus is not None:
            await self._bus.publish(Event(name=name, payload=payload))
