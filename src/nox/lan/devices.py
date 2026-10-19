"""Paired devices in the home network: who may open the dashboard over the network listener.

Pairing reuses the phone companion's machinery (`nox.remote.pairing`): a one-time 8-character code,
valid for a few minutes, only its salted hash stored, redeemed atomically. What differs is what a
redeemed code yields. A device gets a 256-bit token, shown to it exactly once, and the database
keeps only `sha256(salt + "lan:" + token)` - the same rule the phone companion uses for a sender.
Recognising a device is therefore a constant-time comparison against every non-revoked row, and
revoking one takes effect on its very next connection.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from nox.core.events import EventBus
from nox.ipc.tokens import generate_token
from nox.remote.models import DeviceRow, PairingResult, PairingStart
from nox.remote.pairing import PairingService
from nox.remote.repo import RemoteRepository
from nox.security.model import AuditLog

__all__ = ["CHANNEL", "CLIENT_PREFIX", "ROLE", "LanDevices", "Redeemed"]

CHANNEL = "lan"
ROLE = "device"
#: A device connects as `device:<device id>`, so the hub, the audit trail and the permission prompts
#: name the device that acted.
CLIENT_PREFIX = "device:"


@dataclass(frozen=True)
class Redeemed:
    result: PairingResult
    #: Only on success, and only this once.
    token: str | None = None


class LanDevices:
    def __init__(
        self,
        repo: RemoteRepository,
        *,
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
        ttl_s: float = 300.0,
        max_devices: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._clock = clock or (lambda: datetime.now(UTC))
        self.pairing = PairingService(
            repo,
            bus=bus,
            audit=audit,
            ttl_s=ttl_s,
            max_devices=max_devices,
            channel=CHANNEL,
            role=ROLE,
            clock=self._clock,
        )

    async def start(self, *, name: str = "") -> PairingStart:
        return await self.pairing.start(device_name=name or "Device")

    async def redeem(self, code: str, *, name: str = "") -> Redeemed:
        token = generate_token()
        result = await self.pairing.redeem(code, sender_id=token, name=name)
        return Redeemed(result=result, token=token if result.ok else None)

    def verify(self, token: str) -> DeviceRow | None:
        """The paired, non-revoked device this token belongs to; records that it was seen."""
        if not token:
            return None
        device = self._repo.find_device_for_sender(CHANNEL, token)
        if device is None:
            return None
        self._repo.touch_device(device.id, when=self._clock(), update_id=0)
        return self._repo.get_device(device.id)

    async def revoke(self, device_id: str, *, reason: str = "dashboard") -> bool:
        return await self.pairing.revoke(device_id, reason=reason)

    def devices(self) -> list[DeviceRow]:
        return self.pairing.devices()

    def get(self, device_id: str) -> DeviceRow | None:
        device = self._repo.get_device(device_id)
        return device if device is not None and device.channel == CHANNEL else None
