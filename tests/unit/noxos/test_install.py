"""Remote maintenance on Nox-OS (`nox.noxos.install`): setting keys and switching on need a PIN that
exists and is right, from everyone; the keys live with the root helper only and switching on
names the fingerprints the owner confirmed; off needs nothing; every change is audited; outside
Nox-OS nothing is offered."""

from __future__ import annotations

import base64
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
PASS = "correct horse battery staple"  # fictitious
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
        self.passphrase: str | None = PASS
        self.setup_mode = False

    def _answer(self) -> dict[str, Any]:
        keys = [parse_key(line) for line in self.stored]
        until = int(NOW.timestamp()) + 3600
        return {
            "ok": True,
            "ssh": "on" if self.on else "off",
            "until": until if self.on else None,
            "active": [k.fingerprint for k in keys] if self.on else [],
            "keys": [k.view() for k in keys],
            "passphrase": self.passphrase is not None,
            "locked_until": None,
            "setup_mode": self.setup_mode,
        }

    @staticmethod
    def _secret(line: str, label: str) -> str:
        prefix, _, data = line.partition(" ")
        assert prefix == label
        return base64.b64decode(data).decode("utf-8")

    def _check(self, line: str) -> None:
        if self.passphrase is None:
            raise HelperError("no passphrase", refused=True, code="passphrase_missing")
        if self._secret(line, "pass") != self.passphrase:
            raise HelperError("wrong maintenance passphrase", refused=True, code="passphrase_wrong")

    async def __call__(self, lines: list[str]) -> dict[str, Any]:
        self.calls.append(lines)
        if self.fail is not None:
            raise self.fail
        if lines[0] == "maint set":
            if not self.setup_mode:
                raise HelperError("only during setup", refused=True, code="not_setup")
            self.passphrase = self._secret(lines[1], "new")
        elif lines[0] == "maint change":
            self._check(lines[1])
            self.passphrase = self._secret(lines[2], "new")
        elif lines[0] == "keys set":
            self._check(lines[1])
            self.stored = [ED25519_B] if self.swap else lines[2:]
        elif lines[0] == "keys clear":
            self.stored = []
        elif lines[0].startswith("ssh on "):
            self._check(lines[1])
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
        is_protected=lambda: core.protected.append(True) or True,
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
    listed = await call(
        core, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": PASS, "keys": ED25519}, role
    )
    fingerprints = [k["fingerprint"] for k in listed["keys"]]
    return await call(
        core,
        "noxos.ssh.enable",
        {"pin": PIN, "passphrase": PASS, "minutes": minutes, "fingerprints": fingerprints},
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
    error = await refused(
        elsewhere, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": PASS, "keys": ED25519}
    )
    assert error.code == ERR_UNAVAILABLE and helper.calls == []


async def test_setting_keys_and_switching_on_need_a_pin_that_exists(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path, pin=None)
    for role in ("dashboard", "device"):
        for name, payload in (
            ("noxos.ssh.keys.set", {"passphrase": PASS, "keys": ED25519}),
            ("noxos.ssh.enable", {"passphrase": PASS, "fingerprints": [FP]}),
        ):
            error = await refused(core, name, payload, role)
            assert error.code == ERR_PERMISSION and error.details["reason"] == "pin_not_set"
    assert helper.calls == []


@pytest.mark.parametrize("role", ["dashboard", "shell", "device"])
async def test_both_steps_need_the_right_pin_from_everyone(tmp_path: Path, role: str) -> None:
    core, helper = make_core(tmp_path)
    for pin in (None, "0000"):
        extra = {"pin": pin} if pin else {}
        error = await refused(
            core, "noxos.ssh.keys.set", {"passphrase": PASS, "keys": ED25519} | extra, role
        )
        assert error.code == ERR_PERMISSION
        error = await refused(
            core, "noxos.ssh.enable", {"passphrase": PASS, "fingerprints": [FP]} | extra, role
        )
        assert error.code == ERR_PERMISSION
        if role == "device":
            break  # more wrong PINs would remove the device (a test of its own)
    assert helper.calls == []
    status = await keys_then_on(core, role, minutes=30)
    assert status["on"] is True and status["seconds_left"] == 3600
    line = "pass " + base64.b64encode(PASS.encode()).decode()
    assert helper.calls[-2] == ["keys set", line, ED25519]
    assert helper.calls[-1] == [f"ssh on 30 {FP}", line]


async def test_the_answer_to_setting_keys_shows_their_fingerprints(tmp_path: Path) -> None:
    core, _ = make_core(tmp_path)
    keys = ED25519 + " laptop\n" + ECDSA
    listed = await call(core, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": PASS, "keys": keys})
    assert [(k["fingerprint"], k["comment"]) for k in listed["keys"]] == [
        (FP, "laptop"),
        (parse_key(ECDSA).fingerprint, ""),
    ]
    assert listed["on"] is False  # setting keys switches nothing on


async def test_switching_on_names_exactly_the_confirmed_keys(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    await call(core, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": PASS, "keys": ED25519})
    other = parse_key(ED25519_B).fingerprint
    error = await refused(
        core, "noxos.ssh.enable", {"pin": PIN, "passphrase": PASS, "fingerprints": [other]}
    )
    assert error.code == ERR_UNAVAILABLE and error.details["reason"] == "helper_refused"
    assert helper.on is False
    error = await refused(
        core, "noxos.ssh.enable", {"pin": PIN, "passphrase": PASS, "fingerprints": ["SHA256:x"]}
    )
    assert error.code == ERR_VALIDATION and error.details["reason"] == "invalid_fingerprint"
    error = await refused(
        core, "noxos.ssh.enable", {"pin": PIN, "passphrase": PASS, "fingerprints": []}
    )
    assert error.code == ERR_VALIDATION


async def test_a_helper_storing_other_keys_than_sent_is_not_trusted(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    helper.swap = True
    error = await refused(
        core, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": PASS, "keys": ED25519}
    )
    assert error.code == ERR_UNAVAILABLE
    assert core.security.audit.rows[-1]["result"] == "mismatch"


async def test_a_key_file_in_noxs_folder_grants_nothing(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    planted = tmp_path / "noxos" / "ssh_keys.json"
    planted.parent.mkdir(parents=True)
    planted.write_text(f'["{ED25519}"]', "utf-8")
    status = await call(core, "noxos.ssh.status", {})
    assert status["keys"] == []  # the core reads no key file at all
    error = await refused(
        core, "noxos.ssh.enable", {"pin": PIN, "passphrase": PASS, "fingerprints": [FP]}
    )
    assert error.details["reason"] == "helper_refused" and helper.on is False


async def test_a_device_with_wrong_pins_is_removed(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    for _ in range(3):
        await refused(
            core,
            "noxos.ssh.keys.set",
            {"pin": "0000", "passphrase": PASS, "keys": ED25519},
            "device",
        )
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
        await call(core, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": PASS, "keys": keys})
        assert len(helper.calls[-1]) == 5  # the command, the passphrase, three keys
        return
    error = await refused(
        core, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": PASS, "keys": keys}
    )
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
    await refused(core, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": PASS, "keys": RSA})
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
    await refused(
        core, "noxos.ssh.enable", {"pin": "0000", "passphrase": PASS, "fingerprints": [FP]}
    )
    rows = core.security.audit.rows
    assert [(r["action"], r["decision"]) for r in rows] == [("noxos.ssh.enable", "deny")]


@pytest.mark.parametrize("minutes", [0, 4, 241, 10_000])
async def test_the_window_is_bounded(tmp_path: Path, minutes: int) -> None:
    core, helper = make_core(tmp_path)
    payload = {"pin": PIN, "passphrase": PASS, "fingerprints": [FP], "minutes": minutes}
    error = await refused(core, "noxos.ssh.enable", payload)
    assert error.code == ERR_VALIDATION and helper.calls == []


async def test_a_helper_refusal_is_passed_on_and_audited(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    helper.fail = HelperError("remote maintenance could not be switched on", refused=True)
    error = await refused(
        core, "noxos.ssh.enable", {"pin": PIN, "passphrase": PASS, "fingerprints": [FP]}
    )
    assert error.code == ERR_UNAVAILABLE and error.details["reason"] == "helper_refused"
    assert "could not be switched on" in error.message
    assert core.security.audit.rows[-1]["result"] == "failed"
    helper.fail = HelperError("connection refused")
    error = await refused(core, "noxos.ssh.status", {})
    assert error.details["reason"] == "helper_unreachable" and error.retryable


async def test_on_nox_os_the_extension_reports_whether_the_core_is_protected(
    tmp_path: Path,
) -> None:
    # It only asks: protecting happened at the start of `nox core` (test_protect.py).
    core, _ = make_core(tmp_path)
    assert core.protected == [True]
    elsewhere, _ = make_core(tmp_path / "pc", nox_os=False)
    assert elsewhere.protected == []


# ---- the maintenance passphrase ----------------------------------------------------------


async def test_both_steps_need_the_maintenance_passphrase(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    for name, payload in (
        ("noxos.ssh.keys.set", {"pin": PIN, "keys": ED25519}),
        ("noxos.ssh.enable", {"pin": PIN, "fingerprints": [FP]}),
    ):
        error = await refused(core, name, payload)
        assert error.code == ERR_VALIDATION  # no passphrase field at all
    error = await refused(
        core, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": "x" * 20, "keys": ED25519}
    )
    assert error.code == ERR_PERMISSION and error.details["reason"] == "passphrase_wrong"
    assert helper.stored == []


async def test_without_a_passphrase_the_helper_refuses_with_a_clear_reason(
    tmp_path: Path,
) -> None:
    core, helper = make_core(tmp_path)
    helper.passphrase = None
    error = await refused(
        core, "noxos.ssh.keys.set", {"pin": PIN, "passphrase": PASS, "keys": ED25519}
    )
    assert error.details["reason"] == "passphrase_missing"
    status = await call(core, "noxos.ssh.status", {})
    assert status["passphrase_set"] is False


async def test_the_passphrase_never_reaches_the_audit_or_the_answer(tmp_path: Path) -> None:
    core, _ = make_core(tmp_path)
    status = await keys_then_on(core)
    await refused(
        core,
        "noxos.ssh.enable",
        {"pin": PIN, "passphrase": "wrong one, really", "fingerprints": [FP]},
    )
    encoded = base64.b64encode(PASS.encode()).decode()
    for text in (str(core.security.audit.rows), str(status)):
        assert PASS not in text and encoded not in text and "wrong one" not in text


async def test_the_passphrase_is_set_only_during_setup_and_changed_with_the_old_one(
    tmp_path: Path,
) -> None:
    core, helper = make_core(tmp_path)
    helper.passphrase = None
    new = "a new long passphrase"
    error = await refused(core, "noxos.ssh.passphrase.set", {"pin": PIN, "new": new})
    assert error.code == ERR_PERMISSION and error.details["reason"] == "not_setup"
    helper.setup_mode = True
    status = await call(core, "noxos.ssh.passphrase.set", {"pin": PIN, "new": new})
    assert status["passphrase_set"] is True and helper.passphrase == new
    # Too short is refused before the helper sees it.
    error = await refused(core, "noxos.ssh.passphrase.set", {"pin": PIN, "new": "short"})
    assert error.code == ERR_VALIDATION
    error = await refused(
        core, "noxos.ssh.passphrase.change", {"pin": PIN, "old": "not it at all", "new": PASS}
    )
    assert error.details["reason"] == "passphrase_wrong" and helper.passphrase == new
    await call(core, "noxos.ssh.passphrase.change", {"pin": PIN, "old": new, "new": PASS})
    assert helper.passphrase == PASS
    actions = [(r["action"], r["result"]) for r in core.security.audit.rows]
    assert ("noxos.ssh.passphrase.set", "not_setup") in actions
    assert ("noxos.ssh.passphrase.change", "ok") in actions


@pytest.mark.parametrize("name", ["noxos.ssh.passphrase.set", "noxos.ssh.passphrase.change"])
async def test_passphrase_requests_need_the_pin_too(tmp_path: Path, name: str) -> None:
    core, helper = make_core(tmp_path)
    helper.setup_mode = True
    payload = {"new": "a new long passphrase", "old": PASS}
    if name.endswith(".set"):
        payload.pop("old")
    error = await refused(core, name, payload, "device")
    assert error.code == ERR_PERMISSION
    assert helper.passphrase == PASS


async def test_claim_status_is_read_only_and_says_where_there_is_no_claim(tmp_path: Path) -> None:
    core, helper = make_core(tmp_path)
    answer = await call(core, "noxos.claim.status", {})
    assert answer == {
        "available": False,
        "claimed": False,
        "setup_mode": False,
        "seconds_left": None,
    }

    class Gate:
        def required(self) -> bool:
            return True

    core.noxos_claim = SimpleNamespace(gate=Gate())
    helper.setup_mode = True
    answer = await call(core, "noxos.claim.status", {}, role="device")
    assert answer["available"] is True and answer["claimed"] is False
    assert answer["setup_mode"] is True
    assert helper.calls[-1] == ["status"]  # only ever asks; changes nothing
