"""Remote maintenance on Nox-OS (`nox.noxos.install`): on needs a PIN that exists and is right,
from everyone; off needs nothing; the keys are checked before the root helper sees them; every
switch is audited; outside Nox-OS nothing is offered."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.errors import ERR_PERMISSION, ERR_UNAVAILABLE, ERR_VALIDATION, IpcError
from nox.ipc.protocol import Envelope, Kind, Source
from nox.noxos.install import HelperError, KeyStore, install
from nox.security.device_pin import DevicePinGuard
from nox.security.gate import SecurityChangeGate
from nox.security.secrets import InMemorySecretStore, PinManager
from tests.unit.noxos.conftest import ECDSA, ED25519, ED25519_B, RSA

PIN = "4711"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


class Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(self, **row: Any) -> int:
        self.rows.append(row)
        return len(self.rows)


class Helper:
    """The root helper as the core sees it: request lines in, a JSON answer out."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.on = False
        self.fail: HelperError | None = None

    async def __call__(self, lines: list[str]) -> dict[str, Any]:
        self.calls.append(lines)
        if self.fail is not None:
            raise self.fail
        if lines[0].startswith("ssh on "):
            self.on = True
        elif lines[0] == "ssh off":
            self.on = False
        until = int(NOW.timestamp()) + 3600
        return {"ok": True, "ssh": "on" if self.on else "off", "until": until if self.on else None,
                "keys": ["SHA256:x"] if self.on else []}  # fmt: skip


def make_core(tmp_path: Path, *, pin: str | None = PIN, nox_os: bool = True):  # type: ignore[no-untyped-def]
    manager = PinManager(InMemorySecretStore(), prefer_argon2=False)
    if pin:
        manager.set_pin(pin, by="test")
    audit = Audit()
    gate = SecurityChangeGate(manager, required=True, audit=audit)  # type: ignore[arg-type]
    revoked: list[tuple[str, str]] = []

    async def revoke(device_id: str, reason: str) -> None:
        revoked.append((device_id, reason))

    core = SimpleNamespace(
        config=SimpleNamespace(paths=SimpleNamespace(data_dir=str(tmp_path))),
        security=SimpleNamespace(gate=gate, audit=audit),
        registry=RequestRegistry(),
        revoked=revoked,
    )
    core.device_pin_guard = DevicePinGuard(lambda: core.security.gate, revoke)
    helper = Helper()
    core.protected = []
    install(
        core,
        helper=helper,
        available=lambda: (True, "") if nox_os else (False, "not_nox_os"),
        clock=lambda: NOW,
        protect=lambda: core.protected.append(True) or True,
    )
    return core, helper


async def call(core: Any, name: str, payload: dict[str, Any], role: str = "dashboard") -> Any:
    client = "device:abc" if role == "device" else f"{role}:t"
    env = Envelope(kind=Kind.REQUEST, name=name, src=Source(role=role, id=client), payload=payload)  # type: ignore[arg-type]
    reply = await core.registry.dispatch(
        RequestContext(client_id=client, role=role, request=env), env
    )
    if reply.kind is Kind.ERROR:
        raise IpcError.from_payload(reply.payload)
    return reply.payload


async def refused(
    core: Any, name: str, payload: dict[str, Any], role: str = "dashboard"
) -> IpcError:
    with pytest.raises(IpcError) as caught:
        await call(core, name, payload, role)
    return caught.value


async def test_status_says_off_and_where_it_is_not_offered(tmp_path: Path) -> None:
    core, _ = make_core(tmp_path)
    status = await call(core, "noxos.ssh.status", {})
    assert status["available"] is True and status["on"] is False and status["keys"] == []
    assert (status["default_minutes"], status["min_minutes"], status["max_minutes"]) == (60, 5, 240)
    elsewhere, helper = make_core(tmp_path / "pc", nox_os=False)
    status = await call(elsewhere, "noxos.ssh.status", {})
    assert status == status | {"available": False, "reason": "not_nox_os", "on": False}
    error = await refused(elsewhere, "noxos.ssh.enable", {"pin": PIN, "keys": ED25519})
    assert error.code == ERR_UNAVAILABLE and helper.calls == []


async def test_switching_on_needs_a_pin_that_exists(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path, pin=None)
    for role in ("dashboard", "device"):
        error = await refused(core, "noxos.ssh.enable", {"keys": ED25519}, role)
        assert error.code == ERR_PERMISSION and error.details["reason"] == "pin_not_set"
    assert helper.calls == []


@pytest.mark.parametrize("role", ["dashboard", "shell", "device"])
async def test_switching_on_needs_the_right_pin_from_everyone(tmp_path: Path, role: str) -> None:
    core, helper = make_core(tmp_path)
    for pin in (None, "0000"):
        payload = {"keys": ED25519} | ({"pin": pin} if pin else {})
        error = await refused(core, "noxos.ssh.enable", payload, role)
        assert error.code == ERR_PERMISSION
    assert helper.calls == []
    status = await call(
        core, "noxos.ssh.enable", {"pin": PIN, "keys": ED25519, "minutes": 30}, role
    )
    assert status["on"] is True and status["seconds_left"] == 3600
    assert helper.calls[-1][0] == "ssh on 30"
    assert helper.calls[-1][1].startswith("ssh-ed25519 AAAA")


async def test_a_device_with_wrong_pins_is_removed(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    for _ in range(3):
        await refused(core, "noxos.ssh.enable", {"pin": "0000", "keys": ED25519}, "device")
    assert core.revoked == [("abc", "wrong_pin")]
    assert helper.calls == []


@pytest.mark.parametrize(
    ("keys", "reason"),
    [
        (RSA, "type"),
        ('command="sh" ' + ED25519, "type"),
        (ED25519.replace("AAAA", "AAA!"), "damaged"),
        ("ssh-ed25519 " + ECDSA.split()[1], "damaged"),
        (ED25519 + " comment\twith tab", "not_ascii"),
        ("\n".join([ED25519] * 2 + [ED25519_B] + [ECDSA]), ""),
    ],
)
async def test_keys_are_checked_before_the_helper_sees_them(
    tmp_path: Path, keys: str, reason: str
) -> None:
    core, helper = make_core(tmp_path)
    if not reason:  # duplicates collapse; three different keys go to the helper
        await call(core, "noxos.ssh.enable", {"pin": PIN, "keys": keys})
        assert len(helper.calls[-1]) == 4
        return
    error = await refused(core, "noxos.ssh.enable", {"pin": PIN, "keys": keys})
    assert error.code == ERR_VALIDATION
    assert error.details == {"reason": "invalid_key", "key": reason}
    assert helper.calls == []


async def test_no_key_at_all_is_refused(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    error = await refused(core, "noxos.ssh.enable", {"pin": PIN})
    assert error.code == ERR_VALIDATION and error.details["reason"] == "no_key"
    assert helper.calls == []


async def test_keys_are_kept_for_next_time_and_can_be_forgotten(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    await call(core, "noxos.ssh.enable", {"pin": PIN, "keys": ED25519 + " admin@example.invalid"})
    off = await call(core, "noxos.ssh.disable", {})
    assert off["on"] is False and [k["comment"] for k in off["keys"]] == ["admin@example.invalid"]
    # Next time without pasting: the saved key goes to the helper again.
    await call(core, "noxos.ssh.enable", {"pin": PIN})
    assert helper.calls[-1][1].startswith("ssh-ed25519 ")
    off = await call(core, "noxos.ssh.disable", {"forget_keys": True})
    assert off["keys"] == []
    assert KeyStore(tmp_path / "noxos" / "ssh_keys.json").load() == []


async def test_switching_off_needs_nothing(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    await call(core, "noxos.ssh.enable", {"pin": PIN, "keys": ED25519})
    status = await call(core, "noxos.ssh.disable", {}, "device")
    assert status["on"] is False and helper.calls[-1] == ["ssh off"]


async def test_every_switch_is_audited(tmp_path: Path) -> None:
    core, _helper = make_core(tmp_path)
    await call(core, "noxos.ssh.enable", {"pin": PIN, "keys": ED25519, "minutes": 15})
    await call(core, "noxos.ssh.disable", {})
    await refused(core, "noxos.ssh.enable", {"pin": PIN, "keys": RSA})
    rows = [(r["action"], r["decision"], r["result"]) for r in core.security.audit.rows]
    assert rows == [
        ("noxos.ssh.enable", "allow", "ok"),
        ("noxos.ssh.disable", "allow", "ok"),
        ("noxos.ssh.enable", "deny", "invalid_key"),
    ]
    first = core.security.audit.rows[0]
    assert first["actor"] == "dashboard:t" and first["details"]["minutes"] == "15"
    assert first["details"]["keys"].startswith("SHA256:")
    assert "AAAA" not in str(core.security.audit.rows)  # fingerprints, never the key itself


async def test_a_wrong_pin_is_audited_by_the_gate(tmp_path: Path) -> None:
    core, _helper = make_core(tmp_path)
    await refused(core, "noxos.ssh.enable", {"pin": "0000", "keys": ED25519})
    rows = core.security.audit.rows
    assert [(r["action"], r["decision"]) for r in rows] == [("noxos.ssh.enable", "deny")]


@pytest.mark.parametrize("minutes", [0, 4, 241, 10_000])
async def test_the_window_is_bounded(tmp_path: Path, minutes: int) -> None:
    core, helper = make_core(tmp_path)
    error = await refused(
        core, "noxos.ssh.enable", {"pin": PIN, "keys": ED25519, "minutes": minutes}
    )
    assert error.code == ERR_VALIDATION and helper.calls == []


async def test_a_helper_refusal_is_passed_on_and_audited(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    helper.fail = HelperError("remote maintenance could not be switched on", refused=True)
    error = await refused(core, "noxos.ssh.enable", {"pin": PIN, "keys": ED25519})
    assert error.code == ERR_UNAVAILABLE and error.details["reason"] == "helper_refused"
    assert "could not be switched on" in error.message
    assert core.security.audit.rows[-1]["result"] == "failed"
    helper.fail = HelperError("connection refused")
    error = await refused(core, "noxos.ssh.status", {})
    assert error.details["reason"] == "helper_unreachable" and error.retryable


async def test_on_nox_os_the_core_is_out_of_reach_of_its_own_user(tmp_path: Path) -> None:
    core, _ = make_core(tmp_path)
    assert core.protected == [True]
    elsewhere, _ = make_core(tmp_path / "pc", nox_os=False)
    assert elsewhere.protected == []
