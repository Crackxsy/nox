"""Who may approve or change something while Nox controls a node - the linked browser, a
paired device, the local dashboard, a remote session - through the real dispatch path,
and the ways out of a lock that always work with the PIN."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nox.node.hub.world_routes import NodeControl
from nox.security.access import (
    REFUSE_LINKED,
    REFUSE_NO_PIN,
    REFUSE_REMOTE,
    REFUSE_ROLE,
    REFUSE_SATURATED,
    SUB_LINKED,
    SUB_NO_PIN,
    SUB_PIN,
    SUB_SATURATED,
    NeedsPin,
    Refuse,
)
from tests.unit.node.support_sessions import (
    BROWSER,
    LAPTOP,
    LOCAL,
    OTHER_NODE,
    OTHER_PHONE,
    PHONE,
    DeviceGuard,
    Mute,
    PinGate,
    Text,
    conversation_registry,
    locked_control,
    request_ctx,
    request_envelope,
    send_from,
    session_refusal,
    session_report,
    wired_core,
)

ASK_PIN = NeedsPin("x requires the security PIN", SUB_PIN)


@pytest.mark.parametrize(
    ("role", "client", "pin", "remote", "gate", "expected"),
    [
        # 1. the browser on the PC Nox controls: never, not even with the PIN
        ("device", "device:" + BROWSER, "4711", False, PinGate(), REFUSE_LINKED),
        # 2. any other paired device and this machine: with the PIN
        ("device", "device:" + OTHER_PHONE, "4711", False, PinGate(), None),
        ("device", "device:" + OTHER_PHONE, None, False, PinGate(), ASK_PIN),
        ("dashboard", "dashboard:1", "4711", False, PinGate(), None),
        ("shell", "shell:1", "4711", False, PinGate(), None),
        ("dashboard", "dashboard:1", None, False, PinGate(), ASK_PIN),
        # 3. this machine from a remote session, or when that cannot be told: refused
        ("dashboard", "dashboard:1", "4711", True, PinGate(), REFUSE_REMOTE),
        ("dashboard", "dashboard:1", "4711", None, PinGate(), REFUSE_REMOTE),
        # 4. no PIN set: refused for everyone
        ("dashboard", "dashboard:1", "4711", False, PinGate(required=False), REFUSE_NO_PIN),
        ("device", "device:" + OTHER_PHONE, "4711", False, PinGate(required=False), REFUSE_NO_PIN),
        ("dashboard", "dashboard:1", "4711", False, None, REFUSE_NO_PIN),
        ("plugin", "plugin:1", "4711", False, PinGate(), REFUSE_ROLE),
    ],
)
async def test_who_may_approve_or_change_while_nox_controls_a_node(
    role: str,
    client: str,
    pin: str | None,
    remote: bool | None,
    gate: Any,
    expected: Refuse | NeedsPin | None,
) -> None:
    refusal = await session_refusal(
        locked_control(),
        request_ctx(role, client),
        pin,
        action="x",
        gate=gate,
        device_guard=DeviceGuard(gate) if gate is not None else None,
        remote=lambda: remote,
    )
    assert refusal == expected


async def test_without_a_session_nothing_is_asked() -> None:
    control = NodeControl(None, lambda _n: None)
    refusal = await session_refusal(
        control,
        request_ctx("device", "device:" + BROWSER),
        None,
        action="x",
        gate=None,
        device_guard=None,
        remote=lambda: True,
    )
    assert refusal is None


async def test_a_slow_remote_check_never_blocks_and_counts_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as time_module

    from nox.node.hub import session_access

    monkeypatch.setattr(session_access, "REMOTE_CHECK_S", 0.05)

    def slow() -> bool:
        time_module.sleep(0.3)
        return False

    assert await session_access.remote_now(slow) is None


async def test_changes_go_through_the_same_rule_on_the_real_dispatch_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not only "allow" - adding an MCP server, approving a plan, any change."""
    from pydantic import BaseModel

    from nox.ipc.dispatch import RequestContext
    from nox.ipc.protocol import Kind

    core, control, _guard, _audit = wired_core(PinGate())
    ran: list[tuple[str, dict[str, Any]]] = []

    class Add(BaseModel):
        model_config = {"extra": "forbid"}
        command: str = "x"

    def handler(name: str) -> Any:
        async def run(_ctx: Any, p: Any) -> dict[str, Any]:
            ran.append((name, p.model_dump()))
            return {}

        return run

    roles = ("dashboard", "shell", "device")
    core.registry.register("mcp.servers.add", Add, handler("mcp.servers.add"), roles=roles)
    core.registry.register("plans.approve", Add, handler("plans.approve"), roles=roles)
    core.registry.register("state.get", Add, handler("state.get"), roles=roles)

    async def send(name: str, role: str, client: str, payload: dict[str, Any]) -> Any:
        envelope = request_envelope(name, role, client, payload)
        ctx = RequestContext(client_id=client, role=role, request=envelope)
        return await core.registry.dispatch(ctx, envelope)

    local, phone, linked = "dashboard:1", "device:" + OTHER_PHONE, "device:" + BROWSER
    assert (await send("mcp.servers.add", "dashboard", local, {})).kind is Kind.ERROR
    assert (await send("plans.approve", "device", phone, {})).kind is Kind.ERROR
    assert (await send("plans.approve", "device", linked, {"pin": "4711"})).kind is Kind.ERROR
    assert ran == []
    # With the PIN: through, and the PIN is not passed on to a request that takes none.
    assert (
        await send("mcp.servers.add", "dashboard", local, {"pin": "4711"})
    ).kind is Kind.RESPONSE
    assert (await send("plans.approve", "device", phone, {"pin": "4711"})).kind is Kind.RESPONSE
    assert ran == [("mcp.servers.add", {"command": "x"}), ("plans.approve", {"command": "x"})]
    # Reading never asks.
    assert (await send("state.get", "device", linked, {})).kind is Kind.RESPONSE
    # Once the node says the session is over, nothing is asked any more.
    control.observe(LAPTOP, session_report(3, False))
    assert (await send("plans.approve", "device", phone, {})).kind is Kind.RESPONSE


async def test_the_reply_handler_follows_the_rule_and_deny_always_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nox.ipc.handlers.core import CoreHandlers, PermissionReply
    from nox.security.model import Decision

    remote: dict[str, bool | None] = {"value": False}
    core, _control, guard, audit = wired_core(PinGate(), remote=lambda: remote["value"])
    handlers = CoreHandlers(core)  # type: ignore[arg-type]
    linked = request_ctx("device", "device:" + BROWSER)
    phone = request_ctx("device", "device:" + OTHER_PHONE)
    local = request_ctx("dashboard", "dashboard:1")
    allow = PermissionReply(grant_id="g", decision=Decision.ALLOW, pin="4711")
    no_pin = PermissionReply(grant_id="g", decision=Decision.ALLOW)
    deny = PermissionReply(grant_id="g", decision=Decision.DENY)
    assert (await handlers.permission_reply(linked, allow))["ok"] is False  # never
    assert audit == []  # the engine never saw it
    assert (await handlers.permission_reply(linked, deny))["ok"] is True  # deny always
    assert (await handlers.permission_reply(phone, no_pin))["ok"] is False
    assert guard.wrong == 1  # counted towards the device's lockout
    assert (await handlers.permission_reply(phone, allow))["ok"] is True
    assert (await handlers.permission_reply(local, no_pin))["ok"] is False
    assert (await handlers.permission_reply(local, allow))["ok"] is True
    remote["value"] = True
    assert (await handlers.permission_reply(local, allow))["ok"] is False
    assert audit == ["g:deny", "g:allow", "g:allow"]


async def test_the_real_wiring_registers_status_release_and_the_hello_reset() -> None:
    """`wire_guards`, `_register_control_requests` and the connection hook are wired."""
    from nox.node.hub.install import connection_hook

    core, control, _guard, _audit = wired_core(PinGate())
    assert core.registry.get("node.computer.status") is not None
    assert core.registry.get("node.computer.release") is not None
    assert core.registry.policy.condition_names() == ("node_session",)
    connected: list[str] = []

    class Registry:
        async def on_connected(self, session: Any) -> None:
            connected.append(session.device_id)

    class Client:
        async def request(self, method: str, params: Any, timeout_s: float = 0) -> Any:
            connected.append(method)
            return {}

    core.config = SimpleNamespace(
        sensors=SimpleNamespace(game=SimpleNamespace(process_names=["RocketLeague.exe"]))
    )
    hook = connection_hook(core, Registry(), [control])  # type: ignore[arg-type]
    session = SimpleNamespace(device_id=LAPTOP, computer=session_report(1, False), client=Client())
    await hook(session)
    assert connected == [LAPTOP, "nox/games"]
    assert not control.locked(LAPTOP)  # the new run's hello reset the old session


def test_a_remote_session_is_recognised() -> None:
    from nox.node.hub.console import console_is_remote

    def run(out: str) -> Any:
        return lambda _argv: out

    linux = "linux"
    assert console_is_remote(platform=linux, env={"SSH_CONNECTION": "x"}, run=run("")) is True
    local = {"XDG_SESSION_ID": "2"}
    assert console_is_remote(platform=linux, env=local, run=run("no\n")) is False
    assert console_is_remote(platform=linux, env=local, run=run("yes\n")) is True
    assert console_is_remote(platform=linux, env=local, run=run("")) is None
    # A service without a login session (the Pi image): nothing remote about it; the PIN applies.
    assert console_is_remote(platform=linux, env={}, run=run("no")) is False


class _WinDll:
    def __init__(self, *, remote: int = 0, session: int = 1, console: int = 1) -> None:
        self.user32 = SimpleNamespace(GetSystemMetrics=lambda _m: remote)
        self.kernel32 = SimpleNamespace(
            ProcessIdToSessionId=self._session, WTSGetActiveConsoleSessionId=lambda: console
        )
        self._value = session

    def _session(self, _pid: int, out: Any) -> int:
        out._obj.value = self._value
        return 1


def test_a_remote_session_is_recognised_on_windows() -> None:
    from nox.node.hub.console import SM_REMOTESESSION, console_is_remote

    assert SM_REMOTESESSION == 0x1000
    assert console_is_remote(platform="win32", windll=_WinDll(remote=1)) is True  # RDP
    assert console_is_remote(platform="win32", windll=_WinDll()) is False  # at the console
    assert console_is_remote(platform="win32", windll=_WinDll(session=2)) is True  # not it
    # No session attached to the console: unknown - never "local".
    assert console_is_remote(platform="win32", windll=_WinDll(console=0xFFFFFFFF)) is None


# ---- getting out of a lock -----------------------------------------------------------------------


def _saturated() -> NodeControl:
    control = NodeControl(None, lambda _n: None, linked=lambda d: BROWSER if d == LAPTOP else None)
    control.answered(LAPTOP, session_report(2, True))
    control.saturated.add(LAPTOP)
    return control


async def test_saturation_binds_approvals_and_messages_only() -> None:
    control = _saturated()
    gate = PinGate()
    shell = request_ctx("shell", "shell:cli")

    async def refusal(action: str, pin: str | None = "4711") -> Refuse | NeedsPin | None:
        return await session_refusal(
            control, shell, pin, action=action, gate=gate, device_guard=None, remote=lambda: False
        )

    assert await refusal("security.permission.reply") is REFUSE_SATURATED
    assert (await refusal("chat.send")) is REFUSE_SATURATED
    assert REFUSE_SATURATED.code == SUB_SATURATED
    assert await refusal("plans.approve") is None  # the PIN rule, not saturation
    assert await refusal("node.unpair", None) is None  # this machine's shell: no PIN
    assert await refusal("node.computer.release") is None
    assert await refusal("security.resume") is None
    wrong = await refusal("node.computer.release", "0000")
    assert wrong is not None and wrong.code == SUB_PIN


async def test_the_escapes_work_with_the_pin_even_remotely_and_from_the_linked_browser() -> None:
    control = _saturated()
    gate = PinGate()

    linked = request_ctx("device", "device:" + BROWSER, "192.0.2.5")

    async def escape(ctx: Any, pin: str | None, action: str, gate: Any, guard: Any) -> Any:
        return await session_refusal(
            control, ctx, pin, action=action, gate=gate, device_guard=guard, remote=lambda: False
        )

    for action in ("node.unpair", "node.computer.release", "security.resume"):
        assert await escape(linked, "4711", action, gate, DeviceGuard(gate)) is None
        refused = await escape(linked, None, action, gate, DeviceGuard(gate))
        assert refused is not None and refused.code == SUB_PIN
    remote_shell = request_ctx("shell", "shell:ssh")
    assert (
        await session_refusal(
            control,
            remote_shell,
            "4711",
            action="node.computer.release",
            gate=gate,
            device_guard=None,
            remote=lambda: True,  # an SSH session to a headless core
        )
        is None
    )
    no_pin = await escape(remote_shell, None, "security.resume", PinGate(required=False), None)
    assert no_pin is not None and no_pin.code == SUB_NO_PIN


def test_removing_a_node_lifts_its_lock_binding_and_saturation_at_once() -> None:
    control = _saturated()
    control.node_reported[LAPTOP] = ["10.0.0.1"]
    assert control.active() and control.is_saturated()
    assert control.forget(LAPTOP) is True
    assert not control.active() and not control.is_saturated()
    assert LAPTOP not in control.node_reported and LAPTOP not in control.devices


async def test_release_and_unpair_pass_the_dispatch_guard_while_saturated() -> None:
    from nox.ipc.protocol import Kind

    core, control, ran = conversation_registry()
    control.saturated.add(LAPTOP)

    async def unpair(_ctx: Any, _p: Any) -> dict[str, Any]:
        ran.append("node.unpair")
        return {}

    core.registry.register("node.unpair", Text, unpair, roles=("dashboard", "shell", "device"))
    reply = await send_from(core, "node.unpair", LOCAL, {}, "")
    assert reply.kind is Kind.RESPONSE and ran == ["node.unpair"]
    released = await send_from(core, "node.computer.release", LOCAL, {"pin": "4711"}, "")
    assert released.kind is Kind.RESPONSE and released.payload["released"] == 1
    refused = await send_from(core, "node.computer.release", LOCAL, {"pin": "0000"}, "")
    assert refused.kind is Kind.ERROR
    assert refused.payload["details"]["sub_reason"] == SUB_PIN


async def test_a_refused_request_carries_a_stable_sub_reason() -> None:
    from nox.ipc.protocol import Kind

    core, _control, _ran = conversation_registry()
    linked = await send_from(core, "plans.approve", "device:" + BROWSER, {"pin": "4711"}, "")
    assert linked.kind is Kind.ERROR
    assert linked.payload["details"] == {"reason": "node_session", "sub_reason": SUB_LINKED}
    assert linked.payload["message"].isascii()  # the English fallback; the dashboard words it
    phone = await send_from(core, "plans.approve", PHONE, {}, "")
    assert phone.payload["details"]["sub_reason"] == SUB_PIN


async def test_the_input_lock_counts_unmuting_as_a_change() -> None:
    """One rule for "calm": what the input lock lets through is what the node lock lets
    through - and unmuting the microphone is not calm."""
    from nox.ipc.dispatch import RequestContext, RequestRegistry
    from nox.ipc.protocol import Kind
    from nox.security.access import InputLock

    registry = RequestRegistry()

    async def mute(_ctx: Any, _p: Any) -> dict[str, Any]:
        return {}

    registry.register("voice.mute", Mute, mute, roles=("dashboard",))
    registry.policy.add_condition(InputLock(lambda: True))
    for muted, kind in ((True, Kind.RESPONSE), (False, Kind.ERROR)):
        envelope = request_envelope("voice.mute", "dashboard", LOCAL, {"muted": muted})
        ctx = RequestContext(client_id=LOCAL, role="dashboard", request=envelope)
        assert (await registry.dispatch(ctx, envelope)).kind is kind


# ---- which device --------------------------------------------------------------------------------


def test_while_a_session_runs_only_its_own_node_may_ask_for_high_risk() -> None:
    control = NodeControl(None, lambda _n: None)
    assert control.may_delegate(OTHER_NODE)
    control.answered(LAPTOP, session_report(2, True))
    assert control.may_delegate(LAPTOP) and not control.may_delegate(OTHER_NODE)
