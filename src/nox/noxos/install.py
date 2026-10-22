"""`install(core)`: remote maintenance (SSH) on Nox-OS, switched from the dashboard.

SSH is off on Nox-OS and `ssh.service` is masked. Switching it on is administration of the
machine itself, so it takes an administrator's request **and** the security PIN - always, also
on the computer Nox runs on, and refused outright while no PIN is set (a headless Pi has nobody
in front of it to vouch for the request). From a paired device the PIN rules of
`nox.security.device_pin` apply (a device is removed after repeated wrong PINs). Switching off
needs nothing: it only ever narrows access. Every switch, and every refusal of someone allowed to
ask at all, is in the audit chain.

The core never changes the system itself; it asks the image's root helper over its socket
(`/run/nox-os/helper/helper.sock`), which knows exactly `status`, `ssh off` and `ssh on <minutes>`
with public keys, answers only the core's main process and checks everything again
(`os/pi-gen/stage-nox/03-hardening/files/nox_os_helper.py`). On: sshd with Nox-OS's own
configuration (key only, `nox-admin` only, private networks only), port 22 open for private
networks, sudo for `nox-admin`, and a timer that ends all of it - open sessions included - after
the chosen time (60 minutes unless asked otherwise, 5 to 240).

Requests, for the UI roles:
  * `noxos.ssh.status` - available here or not, on/off, until when, the keys (fingerprints),
  * `noxos.ssh.enable` - `{pin, minutes, keys?}`; `keys` (pasted public keys) replace the saved
    ones, which are kept in Nox's folder for the next time (public keys, no secret),
  * `noxos.ssh.disable` - `{forget_keys?}`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from nox.core.logging import get_logger
from nox.ipc.dispatch import RequestContext
from nox.ipc.errors import ERR_PERMISSION, ERR_UNAVAILABLE, ERR_VALIDATION, IpcError
from nox.ipc.roles import UI_ROLES
from nox.lan.install import authorize_admin
from nox.noxos.keys import MAX_KEYS, InvalidKeyError, PublicKey, parse_keys

log = get_logger(__name__)

__all__ = [
    "DEFAULT_MINUTES",
    "HELPER_SOCKET",
    "HelperError",
    "KeyStore",
    "install",
    "unix_helper",
]

HELPER_SOCKET = Path("/run/nox-os/helper/helper.sock")
OS_RELEASE = Path("/etc/nox-os-release")
DEFAULT_MINUTES = 60
MIN_MINUTES = 5
MAX_MINUTES = 240
#: The helper starts sshd and a timer and reloads the firewall: give it time on a Pi Zero.
HELPER_TIMEOUT_S = 90.0
MAX_ANSWER_BYTES = 64 * 1024

#: One call to the root helper: request lines in, its JSON answer out.
HelperCall = Callable[[list[str]], Awaitable[dict[str, Any]]]


class HelperError(Exception):
    """The helper could not be reached, or refused (`refused`: its message is safe to show)."""

    def __init__(self, message: str, *, refused: bool = False) -> None:
        super().__init__(message)
        self.refused = refused


def unix_helper(path: Path = HELPER_SOCKET, timeout_s: float = HELPER_TIMEOUT_S) -> HelperCall:
    async def call(lines: list[str]) -> dict[str, Any]:
        if sys.platform == "win32":
            raise HelperError("the Nox-OS helper exists on Nox-OS only")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(path), limit=MAX_ANSWER_BYTES), 5.0
            )
        except (OSError, TimeoutError) as exc:
            raise HelperError(f"the Nox-OS helper is not reachable ({type(exc).__name__})") from exc
        try:
            writer.write(("\n".join(lines) + "\n").encode("ascii"))
            await writer.drain()
            writer.write_eof()
            raw = await asyncio.wait_for(reader.readline(), timeout_s)
        except (OSError, TimeoutError, ValueError) as exc:
            raise HelperError(f"the Nox-OS helper did not answer ({type(exc).__name__})") from exc
        finally:
            writer.close()
        try:
            answer = json.loads(raw)
        except ValueError as exc:
            raise HelperError("the Nox-OS helper gave an answer that cannot be read") from exc
        if not isinstance(answer, dict):
            raise HelperError("the Nox-OS helper gave an answer that cannot be read")
        if answer.get("ok") is not True:
            raise HelperError(str(answer.get("error", "refused"))[:200], refused=True)
        return answer

    return call


class KeyStore:
    """The public keys last used, for the next switch-on. Public keys only - no secret - in Nox's
    own folder, owner-only like everything there."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[PublicKey]:
        try:
            lines = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
            return []
        try:
            return parse_keys(lines)
        except InvalidKeyError:
            return []

    def save(self, keys: list[PublicKey]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([k.line for k in keys]), "utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def forget(self) -> None:
        self._path.unlink(missing_ok=True)


class StatusRequest(BaseModel):
    pass


class EnableRequest(BaseModel):
    pin: str | None = Field(default=None, max_length=128)
    minutes: int = Field(default=DEFAULT_MINUTES, ge=MIN_MINUTES, le=MAX_MINUTES)
    #: Pasted public keys, one per line; replace the saved ones. None: use the saved ones.
    keys: str | None = Field(default=None, max_length=MAX_KEYS * 1100)


class DisableRequest(BaseModel):
    forget_keys: bool = False


def on_nox_os(release: Path = OS_RELEASE, socket_path: Path = HELPER_SOCKET) -> tuple[bool, str]:
    if not release.is_file():
        return False, "not_nox_os"
    if not socket_path.exists():
        return False, "helper_missing"
    return True, ""


def _view(answer: dict[str, Any], saved: list[PublicKey], now: datetime) -> dict[str, Any]:
    on = answer.get("ssh") == "on"
    until = answer.get("until")
    until_dt = datetime.fromtimestamp(until, UTC) if on and isinstance(until, int) else None
    return {
        "available": True,
        "reason": "",
        "on": on,
        "until": until_dt.isoformat() if until_dt else None,
        "seconds_left": max(0, int((until_dt - now).total_seconds())) if until_dt else None,
        "active_keys": [str(k) for k in answer.get("keys", []) if isinstance(k, str)][:MAX_KEYS],
        "keys": [k.view() for k in saved],
        "default_minutes": DEFAULT_MINUTES,
        "min_minutes": MIN_MINUTES,
        "max_minutes": MAX_MINUTES,
    }


def protect_core() -> bool:
    """Make this process non-dumpable (Linux `PR_SET_DUMPABLE` 0).

    The root helper trusts the main process of nox.service, not the user `nox`: commands Nox runs
    for someone run as that user too. Without this, such a process could trace the core or read
    and write its memory (`/proc/<pid>/mem`) and speak to the helper as the core - Raspberry Pi
    OS's kernel has no Yama to stop it. Non-dumpable, the core is out of their reach; it changes
    nothing for the core itself. Children that start another program are dumpable again."""
    if sys.platform != "linux":
        return False
    import ctypes  # noqa: PLC0415 - Linux only

    pr_set_dumpable = 4
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return int(libc.prctl(pr_set_dumpable, 0, 0, 0, 0)) == 0
    except (OSError, AttributeError):
        return False


def install(
    core: Any,
    *,
    helper: HelperCall | None = None,
    available: Callable[[], tuple[bool, str]] = on_nox_os,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    protect: Callable[[], bool] = protect_core,
) -> None:
    store = KeyStore(Path(core.config.paths.data_dir) / "noxos" / "ssh_keys.json")
    call = helper or unix_helper()
    if available()[0]:
        if protect():
            log.info("noxos.core_protected", note="not dumpable: no trace, no memory access")
        else:
            log.warning("noxos.core_not_protected")

    def audit(ctx: RequestContext, action: str, decision: str, result: str, **details: str) -> None:
        core.security.audit.append(
            actor=ctx.client_id or ctx.role,
            tool="noxos",
            action=action,
            target="ssh",
            decision=decision,
            result=result,
            details=details or None,
        )

    def unavailable(reason: str) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "on": False,
            "until": None,
            "seconds_left": None,
            "active_keys": [],
            "keys": [],
            "default_minutes": DEFAULT_MINUTES,
            "min_minutes": MIN_MINUTES,
            "max_minutes": MAX_MINUTES,
        }

    def require_available() -> None:
        ok, reason = available()
        if not ok:
            raise IpcError(
                ERR_UNAVAILABLE,
                "remote maintenance exists on Nox-OS only",
                details={"reason": reason},
            )

    async def ask(lines: list[str]) -> dict[str, Any]:
        try:
            return await call(lines)
        except HelperError as exc:
            log.warning("noxos.helper_failed", refused=exc.refused, error=str(exc))
            raise IpcError(
                ERR_UNAVAILABLE,
                str(exc) if exc.refused else "the Nox-OS helper is not reachable",
                retryable=not exc.refused,
                details={"reason": "helper_refused" if exc.refused else "helper_unreachable"},
            ) from exc

    async def h_status(_ctx: RequestContext, _p: StatusRequest) -> dict[str, Any]:
        ok, reason = available()
        if not ok:
            return unavailable(reason)
        answer = await ask(["status"])
        saved = await asyncio.to_thread(store.load)
        return _view(answer, saved, clock())

    async def h_enable(ctx: RequestContext, p: EnableRequest) -> dict[str, Any]:
        require_available()
        # A PIN must exist: without one there is nothing to prove, and a Pi has nobody at it.
        if not core.security.gate.is_required():
            raise IpcError(
                ERR_PERMISSION,
                "remote maintenance needs the security PIN; set a PIN first",
                details={"reason": "pin_not_set"},
            )
        await authorize_admin(core, ctx, p.pin, "noxos.ssh.enable", local_needs_pin=True)
        try:
            keys = parse_keys(p.keys) if p.keys is not None else await asyncio.to_thread(store.load)
        except InvalidKeyError as exc:
            audit(ctx, "noxos.ssh.enable", "deny", "invalid_key", reason=exc.reason)
            raise IpcError(
                ERR_VALIDATION, str(exc), details={"reason": "invalid_key", "key": exc.reason}
            ) from exc
        if not keys:
            raise IpcError(
                ERR_VALIDATION,
                "remote maintenance needs a public key (ssh-keygen -t ed25519)",
                details={"reason": "no_key"},
            )
        fingerprints = ",".join(k.fingerprint for k in keys)
        try:
            answer = await ask([f"ssh on {p.minutes}", *(k.line for k in keys)])
        except IpcError:
            audit(
                ctx,
                "noxos.ssh.enable",
                "allow",
                "failed",
                minutes=str(p.minutes),
                keys=fingerprints,
            )
            raise
        if p.keys is not None:
            await asyncio.to_thread(store.save, keys)
        audit(ctx, "noxos.ssh.enable", "allow", "ok", minutes=str(p.minutes), keys=fingerprints)
        log.warning("noxos.ssh_on", minutes=p.minutes, keys=len(keys), by=ctx.role)
        return _view(answer, keys, clock())

    async def h_disable(ctx: RequestContext, p: DisableRequest) -> dict[str, Any]:
        require_available()
        try:
            answer = await ask(["ssh off"])
        except IpcError:
            audit(ctx, "noxos.ssh.disable", "allow", "failed")
            raise
        if p.forget_keys:
            await asyncio.to_thread(store.forget)
        audit(ctx, "noxos.ssh.disable", "allow", "ok", forget_keys=str(p.forget_keys).lower())
        log.info("noxos.ssh_off", by=ctx.role)
        return _view(answer, await asyncio.to_thread(store.load), clock())

    reg = core.registry.register
    reg("noxos.ssh.status", StatusRequest, h_status, roles=UI_ROLES)
    reg("noxos.ssh.enable", EnableRequest, h_enable, roles=UI_ROLES)
    reg("noxos.ssh.disable", DisableRequest, h_disable, roles=UI_ROLES)
