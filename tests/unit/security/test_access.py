"""`nox.security.access`: the verdicts, the client, the PIN checks and the policy on their own,
with conditions that are plain fakes. The two real conditions are pinned by `test_access_table`."""

from __future__ import annotations

from typing import Any

import pytest

from nox.ipc.dispatch import client_context
from nox.ipc.errors import ERR_PERMISSION, IpcError
from nox.security.access import (
    ALLOW,
    APPROVAL,
    CALM_UI_REQUESTS,
    CONVERSATION_UI_REQUESTS,
    REFUSE_NO_PIN,
    REFUSE_ROLE,
    AccessPolicy,
    Allow,
    Approval,
    Call,
    ClientContext,
    ClientKind,
    InputLock,
    NeedsPin,
    PeerClass,
    PinChecks,
    Refuse,
    Verdict,
    calm_call,
    calm_ui_request,
    conversation_request,
    peer_class_of,
)
from nox.security.gate import PinRequiredError
from nox.security.model import Risk

DASHBOARD = client_context("dashboard:1", "dashboard")
PHONE = client_context("device:" + "e" * 32, "device", "192.168.1.20")
PLUGIN = client_context("plugin:1", "plugin")


# ---- what is calm ------------------------------------------------------------------------------


def test_the_calm_requests_are_exactly_these() -> None:
    """Adding a name here is a reviewed decision: it stays possible while Nox may be clicking."""
    assert (
        frozenset(
            {
                "state.get",
                "health.get",
                "health.history",
                "config.effective",
                "config.get",
                "security.kill",
                "security.panic",
                "security.pin.status",
                "voice.mute",
                "plugin.status",
                "secrets.status",
                "security.permission.reply",
                "security.permission.pending",
                "personality.get",
                "day.today",
            }
        )
        == CALM_UI_REQUESTS
    )
    assert frozenset({"chat.send", "voice.ptt"}) == CONVERSATION_UI_REQUESTS


@pytest.mark.parametrize(
    ("name", "calm"),
    [
        ("state.get", True),
        ("ipc.ping", True),  # the `ipc.` namespace
        ("plans.list", True),  # a `.list`
        ("lan.status", True),  # a `.status`
        ("plans.approve", False),
        ("config.set", False),
        ("chat.send", False),  # talking is its own class
        ("statelist", False),  # not a suffix of a segment
    ],
)
def test_a_request_is_calm_by_list_namespace_or_suffix(name: str, calm: bool) -> None:
    assert calm_ui_request(name) is calm
    assert conversation_request(name) is (name in {"chat.send", "voice.ptt"})


@pytest.mark.parametrize(
    ("payload", "calm"),
    [
        ({"muted": True}, True),
        ({"muted": False}, False),  # unmuting could give Nox an ear again
        ({"muted": "true"}, False),
        ({"muted": 1}, False),
        ({"muted": None}, False),
        ({}, False),
        (None, False),
        ("muted", False),
    ],
)
def test_muting_is_calm_only_for_a_literal_true(payload: Any, calm: bool) -> None:
    assert calm_call("voice.mute", payload) is calm


# ---- who is asking -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        ("", PeerClass.LOCAL),
        ("127.0.0.1", PeerClass.LOCAL),
        ("::1", PeerClass.LOCAL),
        ("192.168.1.20", PeerClass.LAN),
        ("10.0.0.5", PeerClass.LAN),
        ("[fe80::1]", PeerClass.LAN),
        ("100.101.102.103", PeerClass.TAILNET),
        ("100.64.0.1", PeerClass.TAILNET),
        ("100.127.255.254", PeerClass.TAILNET),
        ("100.63.255.255", PeerClass.LAN),  # just below the shared address space
        ("100.128.0.1", PeerClass.LAN),  # just above it
        ("fd7a:115c:a1e0::1", PeerClass.TAILNET),
        ("fd7b:115c:a1e0::1", PeerClass.LAN),
        ("not an address", PeerClass.LAN),  # unreadable: not this machine
    ],
)
def test_where_a_connection_comes_from(peer: str, expected: PeerClass) -> None:
    assert peer_class_of(peer) is expected


def test_a_client_is_what_its_role_makes_it() -> None:
    assert (DASHBOARD.kind, DASHBOARD.device_id) == (ClientKind.LOCAL_UI, "")
    assert client_context("shell:1", "shell").kind is ClientKind.LOCAL_UI
    assert (PHONE.kind, PHONE.device_id) == (ClientKind.DEVICE, "e" * 32)
    assert (PHONE.peer, PHONE.peer_class) == ("192.168.1.20", PeerClass.LAN)
    assert PLUGIN.kind is ClientKind.OTHER and not PLUGIN.is_person_ui
    assert DASHBOARD.is_person_ui and PHONE.is_person_ui


def test_every_ui_role_is_held_to_the_locks() -> None:
    """A role added to `UI_ROLES` must never fall out of the rule as "anything else"."""
    from nox.ipc.roles import UI_ROLES

    for role in UI_ROLES:
        assert client_context("x:1", role).is_person_ui, role
    assert not client_context("x:1", "plugin").is_person_ui


def test_a_role_added_to_the_ui_roles_is_held_to_the_locks_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nox.ipc.dispatch as dispatch
    from nox.ipc.roles import UI_ROLES

    assert not client_context("tablet:1", "tablet").is_person_ui
    monkeypatch.setattr(dispatch, "UI_ROLES", (*UI_ROLES, "tablet"))
    assert client_context("tablet:1", "tablet").is_person_ui


# ---- the verdicts ------------------------------------------------------------------------------


def test_a_refusal_becomes_wire_details_only_with_a_category() -> None:
    assert Refuse("no").wire_details() is None
    assert Refuse("no", "why", "node_session").wire_details() == {
        "reason": "node_session",
        "sub_reason": "why",
    }
    assert Refuse("no", "", "node_session").wire_details() == {"reason": "node_session"}
    assert NeedsPin("pin", "p", "node_session").wire_details() == {
        "reason": "node_session",
        "sub_reason": "p",
    }


# ---- the policy --------------------------------------------------------------------------------


class Fake:
    """A condition that answers a fixed verdict and notes what it was asked."""

    def __init__(
        self, name: str, verdict: Verdict = ALLOW, *, precedence: int = 50, reads_pin: bool = False
    ) -> None:
        self.name, self.precedence, self.reads_pin = name, precedence, reads_pin
        self.wire_reason = name
        self.verdict = verdict
        self.asked: list[Call] = []
        self.taints = False

    async def check(self, client: ClientContext, call: Call) -> Verdict:
        self.asked.append(call)
        return self.verdict

    def chat_untrusted(self, client: ClientContext) -> bool:
        return self.taints


async def test_without_conditions_everything_is_allowed() -> None:
    assert await AccessPolicy().decide(DASHBOARD, "plans.approve", {}) == Allow()


async def test_only_a_person_ui_is_held_to_a_condition_and_calm_calls_are_never_asked() -> None:
    policy, refuse = AccessPolicy(), Fake("lock", Refuse("no"))
    policy.add_condition(refuse)
    assert isinstance(await policy.decide(DASHBOARD, "plans.approve", {}), Refuse)
    assert isinstance(await policy.decide(PHONE, "plans.approve", {}), Refuse)
    assert await policy.decide(PLUGIN, "plans.approve", {}) == Allow()  # not a person's UI
    assert await policy.decide(DASHBOARD, "state.get", {}) == Allow()  # calm
    assert await policy.decide(DASHBOARD, "voice.mute", {"muted": True}) == Allow()
    assert isinstance(await policy.decide(DASHBOARD, "voice.mute", {"muted": False}), Refuse)
    assert len(refuse.asked) == 3  # the three that were held to it


async def test_the_first_refusal_decides_and_is_stamped_with_its_condition() -> None:
    policy = AccessPolicy()
    first, second = Fake("first", Refuse("one", "c1")), Fake("second", Refuse("two"))
    policy.add_condition(first)
    policy.add_condition(second)
    verdict = await policy.decide(DASHBOARD, "plans.approve", {})
    assert verdict == Refuse("one", "c1", "first")
    assert second.asked == []  # never reached


async def test_conditions_speak_by_precedence_not_by_installation_order() -> None:
    for order in (("early", "late"), ("late", "early")):
        policy = AccessPolicy()
        made = {
            "early": Fake("early", Refuse("early"), precedence=10),
            "late": Fake("late", Refuse("late"), precedence=20),
        }
        for name in order:
            policy.add_condition(made[name])
        assert policy.condition_names() == ("early", "late")
        verdict = await policy.decide(DASHBOARD, "plans.approve", {})
        assert isinstance(verdict, Refuse) and verdict.reason == "early"


def test_a_condition_is_installed_once() -> None:
    policy = AccessPolicy()
    policy.add_condition(Fake("lock"))
    with pytest.raises(ValueError, match="already installed"):
        policy.add_condition(Fake("lock"))


async def test_the_pin_counts_as_read_only_when_a_condition_that_reads_it_ran() -> None:
    policy = AccessPolicy()
    policy.add_condition(Fake("plain"))
    assert await policy.decide(DASHBOARD, "plans.approve", {"pin": "1"}) == Allow(pin_read=False)
    policy.add_condition(Fake("reader", reads_pin=True))
    assert await policy.decide(DASHBOARD, "plans.approve", {"pin": "1"}) == Allow(pin_read=True)


async def test_the_pin_is_handed_to_the_conditions_only_when_it_is_a_string() -> None:
    policy, seen = AccessPolicy(), Fake("lock")
    policy.add_condition(seen)
    for payload in ({"pin": "4711"}, {"pin": 4711}, {}, None, ["pin"]):
        await policy.decide(DASHBOARD, "plans.approve", payload)
    assert [call.pin for call in seen.asked] == ["4711", None, None, None, None]


def test_a_message_is_untrusted_when_any_condition_says_so() -> None:
    policy, quiet, tainting = AccessPolicy(), Fake("quiet"), Fake("tainting")
    policy.add_condition(quiet)
    assert not policy.chat_untrusted(DASHBOARD)
    policy.add_condition(tainting)
    tainting.taints = True
    assert policy.chat_untrusted(DASHBOARD)


async def test_the_input_lock_speaks_only_while_it_holds_and_words_each_surface() -> None:
    held = [False]
    lock = InputLock(lambda: held[0])
    assert await lock.check(DASHBOARD, Call("plans.approve")) == ALLOW
    held[0] = True
    request = await lock.check(DASHBOARD, Call("plans.approve"))
    prompt = await lock.check(DASHBOARD, Call(APPROVAL))
    assert isinstance(request, Refuse) and "end it with the stop hotkey" in request.reason
    assert isinstance(prompt, Refuse) and "press the stop hotkey, then allow" in prompt.reason
    assert not request.code and not prompt.code  # no sub-reason: the dashboard words these itself
    assert not lock.chat_untrusted(DASHBOARD)


class Broken(Fake):
    """A condition that fails while it thinks."""

    async def check(self, client: ClientContext, call: Call) -> Verdict:
        raise RuntimeError("the rule broke")

    def chat_untrusted(self, client: ClientContext) -> bool:
        raise RuntimeError("the rule broke")


async def test_a_failing_condition_stops_the_decision_it_never_waves_a_request_through() -> None:
    """Fail closed: an error inside a condition reaches the caller (the hub answers `internal`);
    the conditions after it are not asked, and nothing is read as "allowed"."""
    policy, after = AccessPolicy(), Fake("after")
    policy.add_condition(Broken("broken", precedence=10))
    policy.add_condition(after)
    with pytest.raises(RuntimeError):
        await policy.decide(DASHBOARD, "plans.approve", {})
    with pytest.raises(RuntimeError):
        await policy.approval(DASHBOARD, answer())
    with pytest.raises(RuntimeError):
        policy.chat_untrusted(DASHBOARD)
    assert after.asked == []


async def test_a_failing_condition_keeps_the_handler_from_running() -> None:
    from nox.ipc.dispatch import EmptyPayload, RequestContext, RequestRegistry
    from nox.ipc.protocol import Envelope, Kind, Source

    ran: list[str] = []

    async def handler(_ctx: Any, _p: Any) -> dict[str, Any]:
        ran.append("plans.approve")
        return {}

    registry = RequestRegistry()
    registry.register("plans.approve", EmptyPayload, handler, roles=("dashboard",))
    registry.policy.add_condition(Broken("broken"))
    envelope = Envelope(
        kind=Kind.REQUEST, name="plans.approve", src=Source(role="dashboard", id="dashboard:1")
    )
    ctx = RequestContext(client_id="dashboard:1", role="dashboard", request=envelope)
    with pytest.raises(RuntimeError):
        await registry.dispatch(ctx, envelope)
    assert ran == []


# ---- answers to permission prompts -------------------------------------------------------------


class Door:
    """A paired device's PIN door: refuses unless the PIN is `4711`."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str | None, str]] = []

    async def require(self, client: ClientContext, pin: str | None, action: str) -> None:
        self.asked.append((client.client_id, pin, action))
        if pin != "4711":
            raise IpcError(ERR_PERMISSION, "wrong", details={"reason": "pin_wrong"})


def answer(*, allow: bool = True, pin: str | None = None, risk: Risk | None = None) -> Approval:
    return Approval(allow=allow, pin=pin, risk=lambda: risk)


async def test_a_deny_always_counts_and_asks_nobody() -> None:
    policy, refuse = AccessPolicy(), Fake("lock", Refuse("no"))
    policy.add_condition(refuse)

    def never() -> Risk | None:
        raise AssertionError("the risk of a prompt is only read when it matters")

    deny = Approval(allow=False, pin=None, risk=never)
    assert await policy.approval(PHONE, deny) == Allow()
    assert refuse.asked == []


async def test_an_allow_is_asked_of_every_client_not_only_of_a_persons_ui() -> None:
    policy, refuse = AccessPolicy(), Fake("lock", Refuse("no"))
    policy.add_condition(refuse)
    for client in (DASHBOARD, PHONE, PLUGIN):
        assert isinstance(await policy.approval(client, answer()), Refuse)
    assert [call.name for call in refuse.asked] == [APPROVAL] * 3


@pytest.mark.parametrize("risk", [Risk.HIGH, Risk.CRITICAL])
async def test_a_device_needs_its_pin_for_a_risky_allow(risk: Risk) -> None:
    policy, door = AccessPolicy(), Door()
    policy.pins.device_guard = door
    assert await policy.approval(PHONE, answer(pin="4711", risk=risk)) == Allow()
    with pytest.raises(IpcError) as refused:
        await policy.approval(PHONE, answer(pin="0000", risk=risk))
    assert refused.value.details == {"reason": "pin_wrong"}  # the door's own error
    assert [action for _c, _p, action in door.asked] == [APPROVAL, APPROVAL]


@pytest.mark.parametrize("risk", [None, Risk.READ, Risk.LOW, Risk.MEDIUM])
async def test_a_device_needs_no_pin_below_high_risk_and_a_local_ui_never(
    risk: Risk | None,
) -> None:
    policy, door = AccessPolicy(), Door()
    policy.pins.device_guard = door
    assert await policy.approval(PHONE, answer(risk=risk)) == Allow()
    assert await policy.approval(DASHBOARD, answer(risk=Risk.CRITICAL)) == Allow()
    assert door.asked == []


async def test_a_device_without_a_pin_door_cannot_allow_a_risky_prompt() -> None:
    with pytest.raises(IpcError) as refused:
        await AccessPolicy().approval(PHONE, answer(risk=Risk.HIGH))
    assert refused.value.code == ERR_PERMISSION


# ---- the PIN -----------------------------------------------------------------------------------


class Gate:
    def __init__(self, *, required: bool = True, pin: str = "4711") -> None:
        self.required, self.pin, self.asked = required, pin, 0

    def is_required(self) -> bool:
        return self.required

    async def require(self, pin: str | None, *, action: str, by: str) -> None:
        self.asked += 1
        if pin != self.pin:
            raise PinRequiredError(action, f"{action} requires the security PIN")


async def test_this_machines_ui_needs_the_pin_and_nobody_else_is_asked_here() -> None:
    checks = PinChecks(gate=lambda: Gate())
    assert await checks.local_ui(DASHBOARD, "4711", "x") == ALLOW
    wrong = await checks.local_ui(DASHBOARD, "0000", "x")
    assert wrong == NeedsPin("x requires the security PIN", "node_session_pin")
    assert await checks.local_ui(PHONE, "4711", "x") == REFUSE_ROLE
    assert await checks.local_ui(PLUGIN, "4711", "x") == REFUSE_ROLE


@pytest.mark.parametrize("gate", [None, Gate(required=False)])
async def test_without_a_pin_set_nothing_is_approved(gate: Gate | None) -> None:
    checks = PinChecks(gate=lambda: gate)
    assert await checks.local_ui(DASHBOARD, "4711", "x") == REFUSE_NO_PIN
    checks.device_guard = Door()
    assert await checks.device_ui(PHONE, "4711", "x") == REFUSE_NO_PIN


async def test_a_device_with_the_pin_door_and_without_one() -> None:
    checks = PinChecks(gate=lambda: Gate())
    assert await checks.device_ui(PHONE, "4711", "x") == REFUSE_ROLE  # no door: nobody to ask
    checks.device_guard = Door()
    assert await checks.device_ui(PHONE, "4711", "x") == ALLOW
    assert await checks.device_ui(PHONE, "0000", "x") == NeedsPin("wrong", "node_session_pin")


async def test_administration_from_a_device_is_the_doors_and_locally_the_gates() -> None:
    gate = Gate()
    checks = PinChecks(gate=lambda: gate)
    refused = await checks.admin(PHONE, "4711", "lan.pair", local_needs_pin=True)
    assert refused == Refuse("lan.pair is not possible from a paired device")
    checks.device_guard = Door()
    assert await checks.admin(PHONE, "4711", "lan.pair", local_needs_pin=False) == ALLOW
    with pytest.raises(IpcError):  # a device always needs the PIN, whatever `local_needs_pin`
        await checks.admin(PHONE, "0000", "lan.unpair", local_needs_pin=False)
    assert gate.asked == 0
    assert await checks.admin(DASHBOARD, None, "lan.unpair", local_needs_pin=False) == ALLOW
    assert gate.asked == 0  # removing narrows: no PIN, and a locked PIN cannot block it
    assert await checks.admin(DASHBOARD, "4711", "lan.pair", local_needs_pin=True) == ALLOW
    wrong = await checks.admin(DASHBOARD, "0000", "lan.pair", local_needs_pin=True)
    assert wrong == NeedsPin("lan.pair requires the security PIN")
    assert gate.asked == 2


async def test_administration_without_a_gate_is_refused_not_waved_through() -> None:
    verdict = await PinChecks().admin(DASHBOARD, "4711", "lan.pair", local_needs_pin=True)
    assert isinstance(verdict, Refuse)


# ---- the production wiring ---------------------------------------------------------------------


async def test_wire_guards_puts_the_real_remote_probe_into_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit probe the node condition asks `console_is_remote`: this computer used
    through a remote session is refused for talking to Nox, one used at the console is not."""
    from types import SimpleNamespace

    import nox.node.install as install_module
    from nox.ipc.dispatch import RequestRegistry
    from nox.node.world_routes import NodeControl

    answers: list[bool | None] = [False]
    monkeypatch.setattr(install_module, "console_is_remote", lambda: answers[0])
    core = SimpleNamespace(registry=RequestRegistry())
    control = NodeControl(None, lambda _n: None)
    control.answered("a" * 32, {"gen": 2, "active": True, "id": "ab" * 16})
    install_module.wire_guards(core, control)
    policy = core.registry.policy
    assert policy.condition_names() == ("node_session",)
    assert await policy.decide(DASHBOARD, "chat.send", {}) == Allow(pin_read=True)
    for remote in (True, None):  # at a remote session, or when that cannot be told
        answers[0] = remote
        verdict = await policy.decide(DASHBOARD, "chat.send", {})
        assert isinstance(verdict, Refuse) and verdict.code == "node_session_remote"
