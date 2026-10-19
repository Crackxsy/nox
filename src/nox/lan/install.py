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
from collections.abc import Callable
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
from nox.lan.app import LanSettings, PairLimiter, create_lan_app
from nox.lan.devices import CLIENT_PREFIX, LanDevices
from nox.lan.tls import KeyNotProtectedError, TlsMaterial, ensure_tls
from nox.remote.models import DeviceRow
from nox.remote.repo import RemoteRepository
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

    `make_tls` and `build` are injected, so tests drive every path - a resolver that hangs, a key
    that cannot be protected, a certificate re-issued under the same or a new serial - without a
    network, a core or real time.
    """

    def __init__(
        self,
        runtime: LanRuntime,
        *,
        make_tls: Callable[[], TlsMaterial],
        build: Callable[[TlsMaterial], Server],
        timeout_s: float = TLS_TIMEOUT_S,
    ) -> None:
        self._runtime = runtime
        self._make_tls = make_tls
        self._build = build
        self._timeout_s = timeout_s

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
        await self.start()
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.recheck()
            except Exception as exc:  # noqa: BLE001 - the watcher must outlive one bad round
                log.error("lan.recheck_failed", error=f"{type(exc).__name__}: {exc}")


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
    core.tokens.use_device_verifier(lambda token: getattr(devices.verify(token), "id", None))
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
        )
        return HttpServer(
            app,
            LanSettings(host=cfg.host, port=cfg.port),
            ssl_certfile=material.cert_file,
            ssl_keyfile=material.key_file,
            name="lan",
        )

    listener = LanListener(runtime, make_tls=lambda: ensure_tls(tls_dir), build=build)
    task = asyncio.get_running_loop().create_task(listener.run(), name="nox-lan")
    runtime.tasks.add(task)
    task.add_done_callback(runtime.tasks.discard)
    _add_health_check(core, runtime)
    return runtime


async def _close_later(core: Any, device_id: str, delay_s: float = 0.5) -> None:
    await asyncio.sleep(delay_s)
    await core.hub.disconnect_device(device_id, reason="signed out")


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
            raise IpcError(ERR_UNAVAILABLE, "network access is off (ipc.lan.enabled)")
        if runtime.tls is None or runtime.server is None:
            raise IpcError(
                ERR_UNAVAILABLE, runtime.error or "network access is starting", retryable=True
            )
        return runtime.tls

    async def h_status(_ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        return {
            "enabled": runtime.enabled,
            "running": runtime.running,
            "error": runtime.error,
            "warning": runtime.warning,
            "port": runtime.port,
            "urls": runtime.urls(),
            "fingerprint": runtime.tls.fingerprint if runtime.tls else "",
            "ca_url": "/nox-ca.crt",
        }

    async def h_devices(ctx: RequestContext, _p: EmptyRequest) -> dict[str, Any]:
        own = ctx.client_id.removeprefix(CLIENT_PREFIX) if ctx.role == DEVICE_ROLE else ""
        listed = await asyncio.to_thread(devices.devices)
        return {
            "enabled": runtime.enabled,
            "devices": [_device_json(d, current=d.id == own) for d in listed],
        }

    async def authorize(ctx: RequestContext, pin: str | None, action: str) -> None:
        """Pairing and removing devices from a paired device needs the PIN - always, also where
        the local dashboard does not ask for one. Otherwise a lost phone could pair a second
        device it keeps, or lock its owner out. Without a PIN it is refused outright."""
        gate = core.security.gate
        if ctx.role == DEVICE_ROLE and not gate.is_required():
            raise IpcError(
                ERR_PERMISSION,
                f"{action} from a paired device needs the security PIN; set one on the server",
            )
        try:
            await gate.require(pin, action=action, by=ctx.client_id or ctx.role)
        except PinRequiredError as exc:
            raise IpcError(ERR_PERMISSION, exc.reason) from exc

    async def h_pair_start(ctx: RequestContext, p: PairStartRequest) -> dict[str, Any]:
        material = require_running()
        await authorize(ctx, p.pin, "lan.pair")
        start = await devices.start(name=p.name)
        urls = [f"{u}#pair={start.code}" for u in lan_urls(material, runtime.port)]
        if not urls:
            raise IpcError(ERR_UNAVAILABLE, "this machine has no address in the home network")
        expires: datetime = start.expires_at
        return {
            "code": start.code,
            "expires_at": expires.isoformat(),
            "ttl_s": start.ttl_s,
            "url": urls[0],
            "urls": urls,
            "qr": _qr(urls[0]),
            "fingerprint": material.fingerprint,
        }

    async def h_unpair(ctx: RequestContext, p: UnpairRequest) -> dict[str, Any]:
        if devices.get(p.device_id) is None:
            raise IpcError(ERR_NOT_FOUND, "no such device")
        # Signing itself out is always allowed; removing another device is administration.
        itself = ctx.role == DEVICE_ROLE and ctx.client_id == f"{CLIENT_PREFIX}{p.device_id}"
        if not itself:
            await authorize(ctx, p.pin, "lan.unpair")
        ok = await devices.revoke(p.device_id, reason="signed_out" if itself else "dashboard")
        if ok and itself:
            # The token is already dead. Its own sessions close a moment later, so this answer
            # still reaches the device and its dashboard can say "signed out" instead of "lost".
            task = asyncio.get_running_loop().create_task(_close_later(core, p.device_id))
            runtime.tasks.add(task)
            task.add_done_callback(runtime.tasks.discard)
            return {"ok": True, "device_id": p.device_id, "closed_sessions": 0}
        closed = await core.hub.disconnect_device(p.device_id) if ok else 0
        return {"ok": ok, "device_id": p.device_id, "closed_sessions": closed}

    reg = core.registry.register
    reg("lan.status", EmptyRequest, h_status, roles=UI_ROLES)
    reg("lan.devices.list", EmptyRequest, h_devices, roles=UI_ROLES)
    reg("lan.pair.start", PairStartRequest, h_pair_start, roles=UI_ROLES)
    reg("lan.unpair", UnpairRequest, h_unpair, roles=UI_ROLES)
