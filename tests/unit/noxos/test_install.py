"""Remote maintenance on Nox-OS (`nox.noxos.install`): setting keys and switching on need a PIN that
exists and is right, from everyone; the keys live with the root helper only and switching on
names the fingerprints the owner confirmed; off needs nothing; every change is audited; outside
Nox-OS nothing is offered."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nox.ipc.dispatch import RequestContext, RequestRegistry
from nox.ipc.errors import ERR_PERMISSION, ERR_UNAVAILABLE, ERR_VALIDATION, IpcError
from nox.ipc.protocol import Envelope, Kind, Source
from nox.noxos.install import HelperError, install
from nox.noxos.keys import parse_key
from nox.security.device_pin import DevicePinGuard
from nox.security.gate import SecurityChangeGate
from nox.security.secrets import InMemorySecretStore, PinManager
from tests.unit.noxos.conftest import ECDSA, ED25519, ED25519_B, RSA

PIN = "4711"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
FP = parse_key(ED25519).fingerprint


class Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(self, **row: Any) -> int:
        self.rows.append(row)
        return len(self.rows)


class Helper:
    """The root helper as the core sees it, keeping the keys itself as the real one does."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.on = False
        self.stored: list[str] = []
        self.fail: HelperError | None = None
        #: Answer `keys set` with other keys than were sent (a helper gone wrong).
        self.swap = False

    def _answer(self) -> dict[str, Any]:
        keys = [parse_key(line) for line in self.stored]
        until = int(NOW.timestamp()) + 3600
        return {
            "ok": True,
            "ssh": "on" if self.on else "off",
            "until": until if self.on else None,
            "active": [k.fingerprint for k in keys] if self.on else [],
            "keys": [k.view() for k in keys],
        }

    async def __call__(self, lines: list[str]) -> dict[str, Any]:
        self.calls.append(lines)
        if self.fail is not None:
            raise self.fail
        if lines[0] == "keys set":
            self.stored = [ED25519_B] if self.swap else lines[1:]
        elif lines[0] == "keys clear":
            self.stored = []
        elif lines[0].startswith("ssh on "):
            confirmed = sorted(lines[0].split()[3].split(","))
            if confirmed != sorted(parse_key(x).fingerprint for x in self.stored):
                raise HelperError("the stored keys are not the ones confirmed", refused=True)
            self.on = True
        elif lines[0] == "ssh off":
            self.on = False
        return self._answer()


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
        protected=[],
    )
    core.device_pin_guard = DevicePinGuard(lambda: core.security.gate, revoke)
    helper = Helper()
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
    env = Envelope(
        kind=Kind.REQUEST,
        name=name,
        src=Source(role=role, id=client),  # type: ignore[arg-type]
        payload=payload,
    )
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


async def keys_then_on(core: Any, role: str = "dashboard", minutes: int = 60) -> Any:
    listed = await call(core, "noxos.ssh.keys.set", {"pin": PIN, "keys": ED25519}, role)
    fingerprints = [k["fingerprint"] for k in listed["keys"]]
    return await call(
        core,
        "noxos.ssh.enable",
        {"pin": PIN, "minutes": minutes, "fingerprints": fingerprints},
        role,
    )


async def test_status_says_off_and_where_it_is_not_offered(tmp_path: Path) -> None:
    core, _ = make_core(tmp_path)
    status = await call(core, "noxos.ssh.status", {})
    assert status["available"] is True and status["on"] is False and status["keys"] == []
    limits = (status["default_minutes"], status["min_minutes"], status["max_minutes"])
    assert limits == (60, 5, 240)
    elsewhere, helper = make_core(tmp_path / "pc", nox_os=False)
    status = await call(elsewhere, "noxos.ssh.status", {})
    assert status == status | {"available": False, "reason": "not_nox_os", "on": False}
    error = await refused(elsewhere, "noxos.ssh.keys.set", {"pin": PIN, "keys": ED25519})
    assert error.code == ERR_UNAVAILABLE and helper.calls == []


async def test_setting_keys_and_switching_on_need_a_pin_that_exists(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path, pin=None)
    for role in ("dashboard", "device"):
        for name, payload in (
            ("noxos.ssh.keys.set", {"keys": ED25519}),
            ("noxos.ssh.enable", {"fingerprints": [FP]}),
        ):
            error = await refused(core, name, payload, role)
            assert error.code == ERR_PERMISSION and error.details["reason"] == "pin_not_set"
    assert helper.calls == []


@pytest.mark.parametrize("role", ["dashboard", "shell", "device"])
async def test_both_steps_need_the_right_pin_from_everyone(tmp_path: Path, role: str) -> None:
    core, helper = make_core(tmp_path)
    for pin in (None, "0000"):
        extra = {"pin": pin} if pin else {}
        error = await refused(core, "noxos.ssh.keys.set", {"keys": ED25519} | extra, role)
        assert error.code == ERR_PERMISSION
        error = await refused(core, "noxos.ssh.enable", {"fingerprints": [FP]} | extra, role)
        assert error.code == ERR_PERMISSION
        if role == "device":
            break  # more wrong PINs would remove the device (a test of its own)
    assert helper.calls == []
    status = await keys_then_on(core, role, minutes=30)
    assert status["on"] is True and status["seconds_left"] == 3600
    assert helper.calls[-2] == ["keys set", ED25519]
    assert helper.calls[-1] == [f"ssh on 30 {FP}"]


async def test_the_answer_to_setting_keys_shows_their_fingerprints(tmp_path: Path) -> None:
    core, _ = make_core(tmp_path)
    keys = ED25519 + " laptop\n" + ECDSA
    listed = await call(core, "noxos.ssh.keys.set", {"pin": PIN, "keys": keys})
    assert [(k["fingerprint"], k["comment"]) for k in listed["keys"]] == [
        (FP, "laptop"),
        (parse_key(ECDSA).fingerprint, ""),
    ]
    assert listed["on"] is False  # setting keys switches nothing on


async def test_switching_on_names_exactly_the_confirmed_keys(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    await call(core, "noxos.ssh.keys.set", {"pin": PIN, "keys": ED25519})
    other = parse_key(ED25519_B).fingerprint
    error = await refused(core, "noxos.ssh.enable", {"pin": PIN, "fingerprints": [other]})
    assert error.code == ERR_UNAVAILABLE and error.details["reason"] == "helper_refused"
    assert helper.on is False
    error = await refused(core, "noxos.ssh.enable", {"pin": PIN, "fingerprints": ["SHA256:x"]})
    assert error.code == ERR_VALIDATION and error.details["reason"] == "invalid_fingerprint"
    error = await refused(core, "noxos.ssh.enable", {"pin": PIN, "fingerprints": []})
    assert error.code == ERR_VALIDATION


async def test_a_helper_storing_other_keys_than_sent_is_not_trusted(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    helper.swap = True
    error = await refused(core, "noxos.ssh.keys.set", {"pin": PIN, "keys": ED25519})
    assert error.code == ERR_UNAVAILABLE
    assert core.security.audit.rows[-1]["result"] == "mismatch"


async def test_a_key_file_in_noxs_folder_grants_nothing(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    planted = tmp_path / "noxos" / "ssh_keys.json"
    planted.parent.mkdir(parents=True)
    planted.write_text(f'["{ED25519}"]', "utf-8")
    status = await call(core, "noxos.ssh.status", {})
    assert status["keys"] == []  # the core reads no key file at all
    error = await refused(core, "noxos.ssh.enable", {"pin": PIN, "fingerprints": [FP]})
    assert error.details["reason"] == "helper_refused" and helper.on is False


async def test_a_device_with_wrong_pins_is_removed(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    for _ in range(3):
        await refused(core, "noxos.ssh.keys.set", {"pin": "0000", "keys": ED25519}, "device")
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
        await call(core, "noxos.ssh.keys.set", {"pin": PIN, "keys": keys})
        assert len(helper.calls[-1]) == 4
        return
    error = await refused(core, "noxos.ssh.keys.set", {"pin": PIN, "keys": keys})
    assert error.code == ERR_VALIDATION
    assert error.details == {"reason": "invalid_key", "key": reason}
    assert helper.calls == []


async def test_switching_off_and_forgetting_need_nothing(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    await keys_then_on(core)
    status = await call(core, "noxos.ssh.disable", {}, "device")
    assert status["on"] is False and helper.calls[-1] == ["ssh off"]
    assert [k["fingerprint"] for k in status["keys"]] == [FP]
    status = await call(core, "noxos.ssh.disable", {"forget_keys": True}, "device")
    assert status["keys"] == [] and helper.calls[-1] == ["keys clear"]


async def test_every_change_is_audited(tmp_path: Path) -> None:
    core, _helper = make_core(tmp_path)
    await keys_then_on(core, minutes=15)
    await call(core, "noxos.ssh.disable", {})
    await refused(core, "noxos.ssh.keys.set", {"pin": PIN, "keys": RSA})
    rows = [(r["action"], r["decision"], r["result"]) for r in core.security.audit.rows]
    assert rows == [
        ("noxos.ssh.keys.set", "allow", "ok"),
        ("noxos.ssh.enable", "allow", "ok"),
        ("noxos.ssh.disable", "allow", "ok"),
        ("noxos.ssh.keys.set", "deny", "invalid_key"),
    ]
    enable = core.security.audit.rows[1]
    assert enable["actor"] == "dashboard:t" and enable["details"]["minutes"] == "15"
    assert enable["details"]["keys"] == FP
    assert "AAAA" not in str(core.security.audit.rows)  # fingerprints, never the key itself


async def test_a_wrong_pin_is_audited_by_the_gate(tmp_path: Path) -> None:
    core, _helper = make_core(tmp_path)
    await refused(core, "noxos.ssh.enable", {"pin": "0000", "fingerprints": [FP]})
    rows = core.security.audit.rows
    assert [(r["action"], r["decision"]) for r in rows] == [("noxos.ssh.enable", "deny")]


@pytest.mark.parametrize("minutes", [0, 4, 241, 10_000])
async def test_the_window_is_bounded(tmp_path: Path, minutes: int) -> None:
    core, helper = make_core(tmp_path)
    payload = {"pin": PIN, "fingerprints": [FP], "minutes": minutes}
    error = await refused(core, "noxos.ssh.enable", payload)
    assert error.code == ERR_VALIDATION and helper.calls == []


async def test_a_helper_refusal_is_passed_on_and_audited(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    helper.fail = HelperError("remote maintenance could not be switched on", refused=True)
    error = await refused(core, "noxos.ssh.enable", {"pin": PIN, "fingerprints": [FP]})
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
