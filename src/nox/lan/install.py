"""`install(core) -> LanRuntime`: network access wired into a started core.

The request surface is registered always, so the dashboard can say honestly that network access
is off and how to turn it on. The listener itself only starts when `ipc.lan.enabled` is set:

  * certificates from `nox.lan.tls` in `<data_dir>/tls`, made off the event loop with a deadline,
    compared with this machine every ten minutes and re-issued - with a short listener restart -
    when its addresses changed (a new DHCP lease) or the certificate nears its end
    (`LanListener`),
  * the device registry plugged into the hub's token store, so `device` sessions authenticate,
  * the HTTPS listener (`nox.lan.app`) on `ipc.lan.host:port`.

Requests, for the UI roles (`nox.ipc.roles`):
  * `lan.status` - on or off, the addresses, the authority's fingerprint,
  * `lan.devices.list` - paired devices; the caller's own one is marked,
  * `lan.pair.start` - a one-time code, the pairing link and its QR code. Pairing gives a new
    device dashboard access, so it needs the PIN when one is set (`SecurityChangeGate`) - and
    from a paired device always,
  * `lan.unpair` - revoke a device and close its open sessions at once; a device may always sign
    itself out.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import segno
from pydantic import BaseModel, Field

from nox.core.events import HealthStatus
from nox.core.health import Check
from nox.core.logging import get_logger
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_NOT_FOUND, ERR_PERMISSION, ERR_UNAVAILABLE, IpcError
from nox.ipc.http import HttpServer
from nox.ipc.roles import DEVICE_ROLE, UI_ROLES
from nox.ipc.server import CLOSE_POLICY_VIOLATION, CLOSE_SIGNED_OUT
from nox.lan.app import LanSettings, PairLimiter, create_lan_app
from nox.lan.devices import CLIENT_PREFIX, LanDevices
from nox.lan.tls import KeyNotProtectedError, TlsMaterial, ensure_tls
from nox.remote.models import DeviceRow
from nox.remote.repo import RemoteRepository
from nox.security.device_pin import DevicePinGuard
from nox.security.gate import PinRequiredError

log = get_logger(__name__)

__all__ = ["LanListener", "LanRuntime", "install", "lan_urls"]

#: How often the certificate is compared with this machine's current names and addresses.
TLS_RECHECK_S = 600.0
#: Making certificates resolves this machine's own name; a broken resolver must not hold the boot.
TLS_TIMEOUT_S = 15.0


class PairStartRequest(BaseModel):
    name: str = Field(default="", max_length=64)
    pin: str | None = Field(default=None, max_length=128)


class UnpairRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    pin: str | None = Field(default=None, max_length=128)


class EmptyRequest(BaseModel):
    pass


class Server(Protocol):
    """What the listener needs from a server: `HttpServer`, or a stand-in in tests."""

    @property
    def port(self) -> int: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


def lan_urls(material: TlsMaterial, port: int) -> list[str]:
    """Dashboard addresses for other devices, the most reliable first: the machine's own network
    address (works on every phone), then its `.local` name (nicer, but not every network resolves
    it). Loopback is left out - it means "this device" on the phone."""
    hosts = [a for a in material.addresses if a != "127.0.0.1"]
    hosts += [n for n in material.names if n.endswith(".local")]
    return [f"https://{h}:{port}/dashboard/" for h in hosts]


def _qr(url: str) -> str:
    """The pairing link as an SVG data URI (shown as an <img>, never inlined as markup)."""
    return str(
        segno.make(url, error="m").svg_data_uri(scale=6, border=2, dark="#111", light="#fff")
    )


def _device_json(device: DeviceRow, *, current: bool) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "paired_at": device.paired_at.isoformat(),
        "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
        "revoked_at": device.revoked_at.isoformat() if device.revoked_at else None,
        "current": current,
    }


@dataclass
class LanRuntime:
    devices: LanDevices
    enabled: bool
    port: int
    tls: TlsMaterial | None = None
    server: Server | None = None
    #: Why the listener is not serving (it never started, or a restart failed).
    error: str = ""
    #: Why the last certificate check failed while the listener kept serving the old one.
    warning: str = ""
    tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _core: Any = None

    @property
    def running(self) -> bool:
        return self.server is not None

    def urls(self) -> list[str]:
        return lan_urls(self.tls, self.port) if self.tls is not None else []

    async def stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.server is not None:
            await self.server.stop()
            self.server = None
        tokens = getattr(self._core, "tokens", None)
        if tokens is not None:
            tokens.use_device_verifier(None)


class LanListener:
    """Certificates and the HTTPS listener over time: start, re-check, reload.

    `make_tls`, `build` and `sleep` are injected, so tests drive every path - a resolver that
    hangs, a key that cannot be protected, a certificate re-issued under the same or a new serial,
    a round that fails unforeseen - without a network, a core or real time.
    """

    def __init__(
        self,
        runtime: LanRuntime,
        *,
        make_tls: Callable[[], TlsMaterial],
        build: Callable[[TlsMaterial], Server],
        timeout_s: float = TLS_TIMEOUT_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._runtime = runtime
        self._make_tls = make_tls
        self._build = build
        self._timeout_s = timeout_s
        self._sleep = sleep

    async def _certificates(self) -> TlsMaterial | str:
        """Fresh certificates, or why there are none. Off the loop, never longer than the
        deadline: `ensure_tls` resolves this machine's own name."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._make_tls), self._timeout_s)
        except (OSError, ValueError, TimeoutError, KeyNotProtectedError) as exc:
            reason = f"certificates: {type(exc).__name__}: {exc}"
            log.error("lan.tls_failed", error=reason)
            return reason

    async def _serve(self, material: TlsMaterial) -> bool:
        runtime = self._runtime
        server = self._build(material)
        try:
            await server.start()
        except (OSError, RuntimeError, TimeoutError) as exc:
            runtime.error = f"{type(exc).__name__}: {exc}"
            log.error("lan.start_failed", error=runtime.error)
            return False
        runtime.server, runtime.error, runtime.warning = server, "", ""
        runtime.port = server.port
        log.info("lan.ready", urls=runtime.urls(), fingerprint=material.fingerprint)
        return True

    async def start(self) -> bool:
        material = await self._certificates()
        if isinstance(material, str):
            self._runtime.error = material
            return False
        self._runtime.tls = material
        return await self._serve(material)

    async def recheck(self) -> None:
        """Compare the certificate with this machine once. A failed check while serving is a
        warning - the listener keeps the certificate it has - and is cleared by the next good one.
        A new serial (new addresses, a renamed machine, the end of its validity near) means a
        restart, because only a restarted listener serves the new certificate."""
        runtime = self._runtime
        material = await self._certificates()
        if isinstance(material, str):
            if runtime.server is not None:
                runtime.warning = material
            else:
                runtime.error = material
            return
        runtime.warning = ""
        before, runtime.tls = runtime.tls, material
        if runtime.server is not None and before is not None:
            if before.server_serial == material.server_serial:
                return
            log.info("lan.tls_reloaded", addresses=list(material.addresses))
            await runtime.server.stop()
            runtime.server = None
        await self._serve(material)

    async def run(self, interval_s: float = TLS_RECHECK_S) -> None:
        """Start, then re-check forever. No round - the first included - may end the watcher: an
        error it did not foresee is logged and reported, and the next round tries again."""
        step: Callable[[], Awaitable[object]] = self.start
        while True:
            try:
                await step()
            except Exception as exc:  # noqa: BLE001 - the watcher must outlive one bad round
                reason = f"{type(exc).__name__}: {exc}"
                log.error("lan.watcher_round_failed", error=reason)
                if self._runtime.server is None:
                    self._runtime.error = reason
                else:
                    self._runtime.warning = reason
            step = self.recheck
            await self._sleep(interval_s)


def install(core: Any) -> LanRuntime:
    cfg = core.config.ipc.lan
    devices = LanDevices(
        RemoteRepository(core.db),
        bus=core.bus,
        audit=core.security.audit,
        ttl_s=cfg.pair_code_ttl_s,
        max_devices=cfg.max_devices,
    )
    runtime = LanRuntime(devices=devices, enabled=bool(cfg.enabled), port=cfg.port, _core=core)
    _register_requests(core, runtime)
    if not runtime.enabled:
        log.info("lan.disabled", note="network access is off (ipc.lan.enabled)")
        return runtime

    tls_dir = Path(core.config.paths.data_dir) / "tls"

    def device_of(token: str) -> str | None:
        device = devices.verify(token)
        return device.id if device is not None else None

    core.tokens.use_device_verifier(device_of)

    async def revoke(device_id: str, reason: str) -> None:
        """Revoked at once - its token is dead from here on - and its sessions closed a moment
        later, so the answer saying why still reaches it."""
        await devices.revoke(device_id, reason=reason)
        _keep(
            runtime,
            _close_later(core, device_id, reason="device removed", code=CLOSE_POLICY_VIOLATION),
            "nox-lan-remove",
        )

    core.device_pin_guard = DevicePinGuard(lambda: core.security.gate, revoke)
    hub_host = core.hub.settings.host

    def current_tls() -> TlsMaterial:
        assert runtime.tls is not None  # set before any server is built
        return runtime.tls

    def build(material: TlsMaterial) -> Server:
        app = create_lan_app(
            devices=devices,
            tls=current_tls,
            hub_url=lambda: f"ws://{hub_host}:{core.hub.port}/ws",
            bridge_secret=lambda: core.hub.lan_bridge_secret,
            dashboard_dist=core.dashboard_dist if core.dashboard_dist.exists() else None,
            limiter=PairLimiter(per_address=cfg.pair_attempts_per_minute),
            allow_public_clients=cfg.allow_public_clients,
            max_frame_bytes=core.hub.settings.max_frame_bytes,
        )
        return HttpServer(
            app,
            LanSettings(host=cfg.host, port=cfg.port),
            ssl_certfile=material.cert_file,
            ssl_keyfile=material.key_file,
            name="lan",
        )

    listener = LanListener(runtime, make_tls=lambda: ensure_tls(tls_dir), build=build)
    _keep(runtime, listener.run(), "nox-lan")
    _add_health_check(core, runtime)
    return runtime


def _keep(runtime: LanRuntime, coro: Any, name: str) -> None:
    """Run `coro` as a task the runtime holds - and whose failure is logged, never lost."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    runtime.tasks.add(task)

    def done(finished: asyncio.Task[None]) -> None:
        runtime.tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            log.error("lan.task_failed", task=name, error=repr(finished.exception()))

    task.add_done_callback(done)


async def _close_later(
    core: Any,
    device_id: str,
    *,
    reason: str = "signed out",
    code: int = CLOSE_SIGNED_OUT,
    delay_s: float = 0.5,
) -> None:
    """Close a device's sessions a moment after answering it, so the answer arrives first. A
    sign-out closes with a code of its own (4001), so its dashboard says "signed out" even if the
    close wins; a removal closes with 1008 like any revocation."""
    await asyncio.sleep(delay_s)
    await core.hub.disconnect_device(device_id, reason=reason, code=code)


def _add_health_check(core: Any, runtime: LanRuntime) -> None:
    health = getattr(core, "health", None)
    if health is None:
        return

    async def probe() -> tuple[HealthStatus, str]:
        if runtime.server is None:
            if runtime.error:
                return HealthStatus.UNAVAILABLE, runtime.error
            return HealthStatus.LIMITED, "starting"
        if runtime.warning:
            return (
                HealthStatus.LIMITED,
                f"serving; the last certificate check failed: {runtime.warning}",
            )
        listed = await asyncio.to_thread(runtime.devices.devices)
        active = sum(1 for d in listed if not d.revoked)
        return HealthStatus.AVAILABLE, f"{active} paired device(s)"

    with contextlib.suppress(ValueError):  # a second install in one process (tests)
        health.add_check(Check("lan", probe))


def _register_requests(core: Any, runtime: LanRuntime) -> None:
    devices = runtime.devices

    def require_running() -> TlsMaterial:
        if not runtime.enabled:
            raise IpcError(
                ERR_UNAVAILABLE,
                "network access is off (ipc.lan.enabled)",
                details={"reason": "off"},
            )
        if runtime.tls is None or runtime.server is None:
            failed = bool(runtime.error)
            raise IpcError(
                ERR_UNAVAILABLE,
                runtime.error or "network access is starting",
                retryable=True,
                details={"reason": "failed" if failed else "starting"},
            )
        return runtime.tls

    async def h_status(ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        # The raw reason can name local paths; a paired device gets the fact, not the detail.
        local = ctx.role != DEVICE_ROLE
        return {
            "enabled": runtime.enabled,
            "running": runtime.running,
            "error": runtime.error if local else ("unavailable" if runtime.error else ""),
            "warning": runtime.warning if local else "",
            "port": runtime.port,
            "urls": runtime.urls(),
            "fingerprint": runtime.tls.server_fingerprint if runtime.tls else "",
            "ca_fingerprint": runtime.tls.fingerprint if runtime.tls else "",
            "ca_url": "/nox-ca.crt",
            # A paired device can only pair or remove others with the PIN; without one set, the
            # dashboard says where to do it instead of offering a form that cannot work.
            "pin_set": core.security.gate.is_required(),
        }

    async def h_devices(ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        own = ctx.client_id.removeprefix(CLIENT_PREFIX) if ctx.role == DEVICE_ROLE else ""
        listed = await asyncio.to_thread(devices.devices)
        return {
            "enabled": runtime.enabled,
            "devices": [_device_json(d, current=d.id == own) for d in listed],
        }

    async def authorize(
        ctx: RequestContext, pin: str | None, action: str, *, local_needs_pin: bool
    ) -> None:
        """From a paired device: the PIN, always (`DevicePinGuard`, which also removes a device
        after repeated wrong PINs). Locally: the PIN gate as configured where `local_needs_pin`,
        nothing otherwise - removing a device only ever narrows access, and a thief must not be
        able to block it by locking the PIN."""
        if ctx.role == DEVICE_ROLE:
            guard = getattr(core, "device_pin_guard", None)
            if guard is None:
                raise IpcError(ERR_PERMISSION, f"{action} is not possible from a paired device")
            await guard.require(ctx, pin, action)
            return
        if not local_needs_pin:
            return
        try:
            await core.security.gate.require(pin, action=action, by=ctx.client_id or ctx.role)
        except PinRequiredError as exc:
            raise IpcError(ERR_PERMISSION, exc.reason) from exc

    async def h_pair_start(ctx: RequestContext, p: PairStartRequest) -> dict[str, Any]:
        material = require_running()
        await authorize(ctx, p.pin, "lan.pair", local_needs_pin=True)
        start = await devices.start(name=p.name)
        urls = [f"{u}#pair={start.code}" for u in lan_urls(material, runtime.port)]
        if not urls:
            raise IpcError(
                ERR_UNAVAILABLE,
                "this machine has no address in the home network",
                details={"reason": "no_address"},
            )
        expires: datetime = start.expires_at
        return {
            "code": start.code,
            "expires_at": expires.isoformat(),
            "ttl_s": start.ttl_s,
            "url": urls[0],
            "urls": urls,
            "qr": _qr(urls[0]),
            "fingerprint": material.server_fingerprint,
            "ca_fingerprint": material.fingerprint,
        }

    async def h_unpair(ctx: RequestContext, p: UnpairRequest) -> dict[str, Any]:
        if devices.get(p.device_id) is None:
            raise IpcError(ERR_NOT_FOUND, "no such device")
        # Signing itself out is always allowed; removing another device is administration.
        itself = ctx.role == DEVICE_ROLE and ctx.client_id == f"{CLIENT_PREFIX}{p.device_id}"
        if not itself:
            await authorize(ctx, p.pin, "lan.unpair", local_needs_pin=False)
        ok = await devices.revoke(p.device_id, reason="signed_out" if itself else "dashboard")
        if ok and itself:
            # The token is already dead. Its own sessions close a moment later, so this answer
            # still reaches the device and its dashboard can say "signed out" instead of "lost".
            _keep(runtime, _close_later(core, p.device_id), "nox-lan-sign-out")
            return {"ok": True, "device_id": p.device_id, "closed_sessions": 0}
        closed = await core.hub.disconnect_device(p.device_id) if ok else 0
        return {"ok": ok, "device_id": p.device_id, "closed_sessions": closed}

    reg = core.registry.register
    reg("lan.status", EmptyRequest, h_status, roles=UI_ROLES)
    reg("lan.devices.list", EmptyRequest, h_devices, roles=UI_ROLES)
    reg("lan.pair.start", PairStartRequest, h_pair_start, roles=UI_ROLES)
    reg("lan.unpair", UnpairRequest, h_unpair, roles=UI_ROLES)
