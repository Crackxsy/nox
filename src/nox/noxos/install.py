"""`install(core)`: remote maintenance (SSH) on Nox-OS, switched from the dashboard.

SSH is off on Nox-OS and `ssh.service` is masked. Switching it on - and choosing which keys may
sign in - is administration of the machine itself, so both take an administrator's request
**and** the security PIN: always, also on the computer Nox runs on, and refused outright while no
PIN is set (a headless Pi has nobody in front of it to vouch for the request). From a paired
device the PIN rules of `nox.security.device_pin` apply (a device is removed after repeated wrong
PINs). Switching off and forgetting the keys need nothing: they only ever narrow access. Every
change, and every refusal of someone allowed to ask at all, is in the audit chain.

The core never changes the system itself and never keeps a key the helper would trust: it asks
the image's root helper over its socket (`/run/nox-os/helper/helper.sock`,
`os/pi-gen/stage-nox/03-hardening/files/nox_os_helper.py`), which alone stores the keys (root,
0600), checks everything again and answers only the core's main process. The keys are set in a
step of their own, which answers with their fingerprints; the dashboard shows exactly those, and
switching on names them - the helper refuses unless they are exactly the keys it stores. A key
file anywhere the service user can write grants nothing.

On: sshd with Nox-OS's own configuration (key only, `nox-admin` only, private networks only,
never from the device itself), port 22 open for private networks, sudo for `nox-admin`, and a
timer that ends all of it - open sessions included - after the chosen time (60 minutes unless
asked otherwise, 5 to 240).

**The maintenance passphrase.** The security PIN is checked by Nox, and Nox's files can be read
by the service user - a short PIN's hash there could be guessed offline, and code running as that
user would then be one step from root. So setting the keys and switching on also need the
maintenance passphrase (at least 12 characters), which only the root helper checks (scrypt, root
0600, its own lockout). The core passes it through in the one request and never stores, logs or
audits it. It is set during the first-boot setup (`noxos.ssh.passphrase.set`, accepted by the
helper only in setup mode) and changed with the old one (`noxos.ssh.passphrase.change`).

Requests, for the UI roles:
  * `noxos.ssh.status` - available here or not, on/off, until when, the stored keys,
  * `noxos.ssh.keys.set` - `{pin, passphrase, keys}`: pasted public keys replace the stored ones;
    the answer has their fingerprints,
  * `noxos.ssh.enable` - `{pin, passphrase, minutes, fingerprints}`: the confirmed fingerprints,
  * `noxos.ssh.disable` - `{forget_keys?}`,
  * `noxos.ssh.passphrase.set` - `{pin, new}`: only during setup,
  * `noxos.ssh.passphrase.change` - `{pin, old, new}`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
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
from nox.noxos.keys import MAX_KEYS, InvalidKeyError, parse_keys
from nox.noxos.protect import protected

log = get_logger(__name__)

__all__ = [
    "DEFAULT_MINUTES",
    "HELPER_SOCKET",
    "HelperError",
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
FINGERPRINT_PATTERN = r"^SHA256:[A-Za-z0-9+/]{43}$"
MIN_PASSPHRASE = 12
MAX_PASSPHRASE = 256
#: The helper's refusal codes the dashboard words itself (`details.reason`).
HELPER_CODES = frozenset(
    {
        "passphrase_missing",
        "passphrase_wrong",
        "passphrase_locked",
        "passphrase_short",
        "passphrase_exists",
        "not_setup",
    }
)

#: One call to the root helper: request lines in, its JSON answer out.
HelperCall = Callable[[list[str]], Awaitable[dict[str, Any]]]


class HelperError(Exception):
    """The helper could not be reached, or refused (`refused`: its message is safe to show)."""

    def __init__(self, message: str, *, refused: bool = False, code: str = "") -> None:
        super().__init__(message)
        self.refused = refused
        self.code = code


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
            raise HelperError(
                str(answer.get("error", "refused"))[:200],
                refused=True,
                code=str(answer.get("code", ""))[:40],
            )
        return answer

    return call


class StatusRequest(BaseModel):
    pass


def _passphrase(min_length: int = 1) -> Any:
    # Never logged: validation errors carry no input values (`nox.ipc.dispatch`).
    return Field(min_length=min_length, max_length=MAX_PASSPHRASE, repr=False)


def _line(label: str, secret: str) -> str:
    """How the helper takes a passphrase: base64 of its UTF-8, on a line of its own."""
    return f"{label} {base64.b64encode(secret.encode('utf-8')).decode('ascii')}"


class KeysRequest(BaseModel):
    pin: str | None = Field(default=None, max_length=128)
    passphrase: str = _passphrase()
    #: Pasted public keys, one per line; they replace the stored ones.
    keys: str = Field(min_length=1, max_length=MAX_KEYS * 1100)


class EnableRequest(BaseModel):
    pin: str | None = Field(default=None, max_length=128)
    passphrase: str = _passphrase()
    minutes: int = Field(default=DEFAULT_MINUTES, ge=MIN_MINUTES, le=MAX_MINUTES)
    #: The stored keys' fingerprints as the owner saw and confirmed them.
    fingerprints: list[str] = Field(min_length=1, max_length=MAX_KEYS)

    def checked(self) -> list[str]:
        for fingerprint in self.fingerprints:
            if not re.fullmatch(FINGERPRINT_PATTERN, fingerprint):
                raise IpcError(
                    ERR_VALIDATION,
                    "a fingerprint is malformed",
                    details={"reason": "invalid_fingerprint"},
                )
        return sorted(set(self.fingerprints))


class DisableRequest(BaseModel):
    forget_keys: bool = False


class PassphraseSetRequest(BaseModel):
    pin: str | None = Field(default=None, max_length=128)
    new: str = _passphrase(MIN_PASSPHRASE)


class PassphraseChangeRequest(BaseModel):
    pin: str | None = Field(default=None, max_length=128)
    old: str = _passphrase()
    new: str = _passphrase(MIN_PASSPHRASE)


def on_nox_os(release: Path = OS_RELEASE, socket_path: Path = HELPER_SOCKET) -> tuple[bool, str]:
    if not release.is_file():
        return False, "not_nox_os"
    if not socket_path.exists():
        return False, "helper_missing"
    return True, ""


def _keys_of(answer: dict[str, Any]) -> list[dict[str, str]]:
    keys = answer.get("keys")
    out = []
    for key in keys if isinstance(keys, list) else []:
        if isinstance(key, dict) and isinstance(key.get("fingerprint"), str):
            out.append(
                {
                    "type": str(key.get("type", ""))[:64],
                    "fingerprint": str(key["fingerprint"])[:64],
                    "comment": str(key.get("comment", ""))[:100],
                }
            )
    return out[:MAX_KEYS]


def _view(answer: dict[str, Any], now: datetime) -> dict[str, Any]:
    on = answer.get("ssh") == "on"
    until = answer.get("until")
    until_dt = datetime.fromtimestamp(until, UTC) if on and isinstance(until, int) else None
    active = answer.get("active")
    return {
        "available": True,
        "reason": "",
        "on": on,
        "until": until_dt.isoformat() if until_dt else None,
        "seconds_left": max(0, int((until_dt - now).total_seconds())) if until_dt else None,
        "active_keys": [str(k) for k in active if isinstance(k, str)][:MAX_KEYS]
        if isinstance(active, list)
        else [],
        "keys": _keys_of(answer),
        "passphrase_set": answer.get("passphrase") is True,
        "locked_until": datetime.fromtimestamp(locked, UTC).isoformat()
        if isinstance(locked := answer.get("locked_until"), int)
        else None,
        "setup_mode": answer.get("setup_mode") is True,
        "default_minutes": DEFAULT_MINUTES,
        "min_minutes": MIN_MINUTES,
        "max_minutes": MAX_MINUTES,
    }


def install(
    core: Any,
    *,
    helper: HelperCall | None = None,
    available: Callable[[], tuple[bool, str]] = on_nox_os,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    is_protected: Callable[[], bool | None] = protected,
) -> None:
    call = helper or unix_helper()
    if available()[0]:
        # `nox core` made the process non-dumpable before anything started (nox.noxos.protect).
        if is_protected():
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
            "passphrase_set": False,
            "locked_until": None,
            "setup_mode": False,
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

    async def require_admin(ctx: RequestContext, pin: str | None, action: str) -> None:
        # A PIN must exist: without one there is nothing to prove, and a Pi has nobody at it.
        if not core.security.gate.is_required():
            raise IpcError(
                ERR_PERMISSION,
                "remote maintenance needs the security PIN; set a PIN first",
                details={"reason": "pin_not_set"},
            )
        await authorize_admin(core, ctx, pin, action, local_needs_pin=True)

    async def ask(lines: list[str]) -> dict[str, Any]:
        try:
            return await call(lines)
        except HelperError as exc:
            log.warning("noxos.helper_failed", refused=exc.refused, code=exc.code)
            if exc.refused and exc.code in HELPER_CODES:
                raise IpcError(ERR_PERMISSION, str(exc), details={"reason": exc.code}) from exc
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
        return _view(await ask(["status"]), clock())

    async def h_keys(ctx: RequestContext, p: KeysRequest) -> dict[str, Any]:
        require_available()
        await require_admin(ctx, p.pin, "noxos.ssh.keys.set")
        try:
            keys = parse_keys(p.keys)
        except InvalidKeyError as exc:
            audit(ctx, "noxos.ssh.keys.set", "deny", "invalid_key", reason=exc.reason)
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
            answer = await ask(["keys set", _line("pass", p.passphrase), *(k.line for k in keys)])
        except IpcError:
            audit(ctx, "noxos.ssh.keys.set", "allow", "failed", keys=fingerprints)
            raise
        stored = sorted(k["fingerprint"] for k in _keys_of(answer))
        if stored != sorted(k.fingerprint for k in keys):
            # The helper answers with what it stores; anything else is not shown as confirmed.
            audit(ctx, "noxos.ssh.keys.set", "allow", "mismatch", keys=fingerprints)
            raise IpcError(
                ERR_UNAVAILABLE,
                "the stored keys are not the ones sent",
                details={"reason": "helper_refused"},
            )
        audit(ctx, "noxos.ssh.keys.set", "allow", "ok", keys=fingerprints)
        log.warning("noxos.ssh_keys_set", keys=len(keys), by=ctx.role)
        return _view(answer, clock())

    async def h_enable(ctx: RequestContext, p: EnableRequest) -> dict[str, Any]:
        require_available()
        await require_admin(ctx, p.pin, "noxos.ssh.enable")
        confirmed = p.checked()
        joined = ",".join(confirmed)
        try:
            answer = await ask([f"ssh on {p.minutes} {joined}", _line("pass", p.passphrase)])
        except IpcError:
            audit(ctx, "noxos.ssh.enable", "allow", "failed", minutes=str(p.minutes), keys=joined)
            raise
        audit(ctx, "noxos.ssh.enable", "allow", "ok", minutes=str(p.minutes), keys=joined)
        log.warning("noxos.ssh_on", minutes=p.minutes, keys=len(confirmed), by=ctx.role)
        return _view(answer, clock())

    async def h_disable(ctx: RequestContext, p: DisableRequest) -> dict[str, Any]:
        require_available()
        try:
            answer = await ask(["ssh off"])
            if p.forget_keys:
                answer = await ask(["keys clear"])
        except IpcError:
            audit(ctx, "noxos.ssh.disable", "allow", "failed")
            raise
        audit(ctx, "noxos.ssh.disable", "allow", "ok", forget_keys=str(p.forget_keys).lower())
        log.info("noxos.ssh_off", by=ctx.role)
        return _view(answer, clock())

    async def h_passphrase_set(ctx: RequestContext, p: PassphraseSetRequest) -> dict[str, Any]:
        require_available()
        await require_admin(ctx, p.pin, "noxos.ssh.passphrase.set")
        try:
            answer = await ask(["maint set", _line("new", p.new)])
        except IpcError as exc:
            audit(
                ctx, "noxos.ssh.passphrase.set", "allow", str(exc.details.get("reason", "failed"))
            )
            raise
        audit(ctx, "noxos.ssh.passphrase.set", "allow", "ok")
        return _view(answer, clock())

    async def h_passphrase_change(
        ctx: RequestContext, p: PassphraseChangeRequest
    ) -> dict[str, Any]:
        require_available()
        await require_admin(ctx, p.pin, "noxos.ssh.passphrase.change")
        try:
            answer = await ask(["maint change", _line("pass", p.old), _line("new", p.new)])
        except IpcError as exc:
            audit(
                ctx,
                "noxos.ssh.passphrase.change",
                "allow",
                str(exc.details.get("reason", "failed")),
            )
            raise
        audit(ctx, "noxos.ssh.passphrase.change", "allow", "ok")
        return _view(answer, clock())

    reg = core.registry.register
    reg("noxos.ssh.status", StatusRequest, h_status, roles=UI_ROLES)
    reg("noxos.ssh.keys.set", KeysRequest, h_keys, roles=UI_ROLES)
    reg("noxos.ssh.enable", EnableRequest, h_enable, roles=UI_ROLES)
    reg("noxos.ssh.disable", DisableRequest, h_disable, roles=UI_ROLES)
    reg("noxos.ssh.passphrase.set", PassphraseSetRequest, h_passphrase_set, roles=UI_ROLES)
    reg(
        "noxos.ssh.passphrase.change",
        PassphraseChangeRequest,
        h_passphrase_change,
        roles=UI_ROLES,
    )
