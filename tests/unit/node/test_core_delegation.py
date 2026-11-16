"""The core's side of a call on a node: which node "here" means, when the question is
delegated to the device (only to a node that can ask), who decided travelling in the
receipt, how long the executor waits for the device's dialog, and that a paired device's
request never runs on the core instead."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from nox.lan.tls import TlsMaterial
from nox.node.listener import NodeListener
from nox.node.manifest import NodeManifest
from nox.node.pairing import NodePairing
from nox.node.registry import NodeEntry, NodeRegistry
from nox.node.router import ROUTED_TIMEOUT_S
from tests.unit.node.support import (
    Confirmer,
    FakeRegistry,
    TargetArgs,
    call,
    registry_entry,
    session_of,
    start_node,
)
from tests.unit.node.support_sessions import LAPTOP, privacy_state, routed_specs


def test_here_from_a_browser_on_a_node_means_that_node() -> None:
    """Review N5."""
    from nox.node.registry import NodeEntry
    from nox.node.router import resolve

    laptop = NodeEntry(
        device_id="a" * 32,
        name="Laptop",
        online=True,
        tools={"notify.show": "low"},
        linked_device_id="d" * 32,
    )
    other = NodeEntry(device_id="b" * 32, name="PC", online=True, tools={"notify.show": "low"})
    found = resolve(
        "notify.show",
        "hier",
        entries=[laptop, other],
        core_has=False,
        side_effects=True,
        default=None,
        origin="d" * 32,
    )
    assert found.device == "a" * 32
    core = resolve(
        "system.info",
        "hier",
        entries=[laptop],
        core_has=True,
        side_effects=False,
        default=None,
        origin=None,
    )
    assert core.device == "core"


async def test_the_core_delegates_only_to_a_node_that_can_ask() -> None:
    from nox.node.registry import NodeEntry
    from nox.node.router import routed_spec
    from nox.node.tools import NotifyArgs
    from nox.security.model import Risk

    class Registry:
        def __init__(self, entries: list[NodeEntry]) -> None:
            self._e = entries

        def entries(self) -> list[NodeEntry]:
            return self._e

    for confirmer in (True, False):
        entry = NodeEntry(
            device_id="a" * 32,
            name="Laptop",
            online=True,
            tools={"notify.show": "high"},
            confirmer=confirmer,
        )
        spec = routed_spec(
            name="notify.show",
            description="t",
            risk=Risk.LOW,
            side_effects=True,
            registry=Registry([entry]),
            input_model=NotifyArgs,  # type: ignore[arg-type]
        )
        payload = spec.input_model.model_validate({"text": "x"}).model_dump()
        assert spec.scope is not None
        scope = spec.scope(payload)
        assert scope.risk is Risk.HIGH and scope.confirm_on_device is confirmer


async def test_the_executor_skips_its_own_prompt_only_when_delegated() -> None:
    from nox.security.model import Decision, PermissionResult, Risk
    from nox.tools.executor import ToolExecutor
    from nox.tools.registry import CallScope, ToolRegistry, ToolSpec

    class Engine:
        def __init__(self, decision: Decision) -> None:
            self.decision, self.prompts = decision, 0

        def check(self, _request: Any) -> PermissionResult:
            return PermissionResult(decision=self.decision, rule_id="t")

        def request_confirmation(self, _request: Any) -> str:
            self.prompts += 1
            return "g"

        async def await_confirmation(
            self, _gid: str, *, timeout: float | None = None
        ) -> PermissionResult:
            return PermissionResult(decision=Decision.DENY, rule_id="t")

    class Audit:
        def append(self, **_k: Any) -> int:
            return 0

    class Kill:
        def is_engaged(self) -> bool:
            return False

    async def handler(_p: dict[str, Any]) -> dict[str, Any]:
        return {"ran": True}

    for delegated, device, decision, prompts, ok in [
        (True, "a" * 32, Decision.CONFIRM, 0, True),  # left to the device
        (True, "", Decision.CONFIRM, 1, False),  # not a node: the core asks
        (False, "a" * 32, Decision.CONFIRM, 1, False),  # not delegated: the core asks
        (True, "a" * 32, Decision.DENY, 0, False),  # the core's refusal stands
    ]:
        tools = ToolRegistry()
        tools.register(
            ToolSpec(
                name="package.install",
                description="t",
                input_model=TargetArgs,
                risk=Risk.HIGH,
                handler=handler,
                scope=lambda _p, d=device, g=delegated: CallScope(
                    action="install", risk=Risk.HIGH, device=d, confirm_on_device=g
                ),
            )
        )
        engine = Engine(decision)
        executor = ToolExecutor(tools, engine, Audit(), None, Kill())  # type: ignore[arg-type]
        result = await executor.call("companion", "package.install", {}, mode="companion")
        assert engine.prompts == prompts and result.ok is ok, (delegated, device, decision)


async def test_the_handler_never_re_decides_what_the_scope_decided() -> None:
    """Review follow-up (TOCTOU): the node's dialog disappears between the permission scope and
    the handler - the call still goes out exactly as decided (delegated), so the node refuses it
    instead of running it unconfirmed."""
    from nox.node.registry import NodeEntry
    from nox.node.router import routed_spec
    from nox.node.tools import NotifyArgs
    from nox.security.model import Risk

    sent: list[dict[str, Any]] = []

    class Registry:
        def __init__(self, entry: NodeEntry) -> None:
            self.entry = entry

        def entries(self) -> list[NodeEntry]:
            return [self.entry]

        async def call(
            self,
            entry: NodeEntry,
            tool: str,
            arguments: dict[str, Any],
            *,
            risk: str,
            timeout_s: float,
            confirm_on_device: bool = False,
        ) -> dict[str, Any]:
            sent.append({"risk": risk, "confirm_on_device": confirm_on_device, "args": arguments})
            return {"ok": True}

    entry = NodeEntry(
        device_id="a" * 32,
        name="Laptop",
        online=True,
        tools={"notify.show": "high"},
        confirmer=True,
    )
    registry = Registry(entry)
    spec = routed_spec(
        name="notify.show",
        description="t",
        risk=Risk.LOW,
        side_effects=True,
        registry=registry,
        input_model=NotifyArgs,
    )  # type: ignore[arg-type]
    payload = spec.input_model.model_validate({"text": "x"}).model_dump()
    assert spec.scope is not None
    scope = spec.scope(payload)
    assert scope.confirm_on_device is True
    entry.confirmer = False  # the dialog went away ...
    entry.tools["notify.show"] = "low"  # ... and the node now claims less risk
    await spec.handler(payload)
    assert sent == [
        {"risk": "high", "confirm_on_device": True, "args": {"title": "Nox", "text": "x"}}
    ]


async def test_a_delegated_call_without_a_dialog_is_refused_even_when_trusted(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await start_node(
        listener,
        pairing,
        tls,
        tmp_path,
        confirmer=Confirmer(available=False),
        manifest=NodeManifest(trust_core_for_high=True),
    )
    try:
        session = await session_of(listener, node.node_id)
        result = await call(
            session,
            listener.signer,
            "system.high",
            risk="high",
            confirm_on_device=True,  # type: ignore[attr-defined]
        )
        assert result["_nox"]["refused"] == "high_needs_local_confirmation" and not node.ran
        trusted = await call(session, listener.signer, "system.high", risk="high")  # type: ignore[attr-defined]
        assert "_nox" not in trusted and node.ran == ["high"]  # the core confirmed: trusted
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


# ---- M-b: who decided on the core travels in the receipt; trust needs a person ----


@pytest.mark.parametrize("decided_by", ["user", "grant", "profile", "device"])
async def test_trust_core_for_high_needs_a_persons_yes_on_the_core(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path, decided_by: str
) -> None:
    node = await start_node(
        listener,
        pairing,
        tls,
        tmp_path,
        confirmer=Confirmer(available=False),
        manifest=NodeManifest(trust_core_for_high=True),
    )
    try:
        session = await session_of(listener, node.node_id)
        result = await call(
            session,
            listener.signer,
            "system.high",
            risk="high",
            decided_by=decided_by,  # type: ignore[attr-defined]
        )
        assert bool(node.ran) is (decided_by == "user"), result
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def test_the_executor_says_who_decided() -> None:
    from nox.security.model import Decision, PermissionResult, Risk
    from nox.tools.executor import CALL_DECISION, ToolExecutor
    from nox.tools.registry import CallScope, ToolRegistry, ToolSpec

    seen: list[tuple[str, bool]] = []

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def handler(_p: dict[str, Any]) -> dict[str, Any]:
        decision = CALL_DECISION.get()
        assert decision is not None
        seen.append((decision.decided_by, decision.tainted))
        return {}

    class Engine:
        def __init__(self, result: PermissionResult) -> None:
            self.result = result

        def check(self, _r: Any) -> PermissionResult:
            return self.result

        def request_confirmation(self, _r: Any) -> str:
            return "g"

        async def await_confirmation(
            self, _g: str, *, timeout: float | None = None
        ) -> PermissionResult:
            return PermissionResult(decision=Decision.ALLOW, rule_id="confirm.user")

        def is_tainted(self, conversation: str) -> bool:
            return True

    class Audit:
        def append(self, **_k: Any) -> int:
            return 0

    class Kill:
        def is_engaged(self) -> bool:
            return False

    cases = [
        (PermissionResult(decision=Decision.ALLOW, rule_id="default.low"), False, "profile"),
        (
            PermissionResult(decision=Decision.ALLOW, rule_id="grant.x", grant_id="g1"),
            False,
            "grant",
        ),
        (PermissionResult(decision=Decision.CONFIRM, rule_id="default.medium"), False, "user"),
        (PermissionResult(decision=Decision.CONFIRM, rule_id="default.high"), True, "device"),
    ]
    from nox.security.taint import CONVERSATION

    for result, delegated, _expected in cases:
        tools = ToolRegistry()
        tools.register(
            ToolSpec(
                name="notify.show",
                description="t",
                input_model=Args,
                risk=Risk.HIGH,
                handler=handler,
                scope=lambda _p, g=delegated: CallScope(
                    action="show", risk=Risk.HIGH, device="a" * 32, confirm_on_device=g
                ),
            )
        )
        executor = ToolExecutor(tools, Engine(result), Audit(), None, Kill())  # type: ignore[arg-type]
        token = CONVERSATION.set("chat:dashboard:t#1")
        try:
            await executor.call("companion", "notify.show", {}, mode="companion")
        finally:
            CONVERSATION.reset(token)
    assert seen == [(c[2], True) for c in cases]


# ---- M4: a call that waits for the device's dialog -----------------------------------------------


async def test_a_high_call_waits_for_the_dialog_on_the_device() -> None:
    from nox.node.confirm import CONFIRM_TIMEOUT_S
    from nox.node.registry import LISTING_WAIT_S

    asked: list[float] = []

    class Client:
        closed = False

        async def request(self, method: str, params: Any, timeout_s: float = 0) -> Any:
            asked.append(timeout_s)
            return {"content": [], "structuredContent": {"ok": True}}

    registry = NodeRegistry(SimpleNamespace(), SimpleNamespace())  # type: ignore[arg-type]
    entry = NodeEntry(device_id=LAPTOP, name="Laptop", online=True, tools={"shell.run": "high"})
    entry.session = SimpleNamespace(alive=True, client=Client(), take_blob=lambda _c: None)  # type: ignore[assignment]
    await registry.call(entry, "shell.run", {}, risk="high", timeout_s=30)
    await registry.call(entry, "shell.run", {}, risk="low", timeout_s=30)
    assert asked == [30 + CONFIRM_TIMEOUT_S, 30]
    assert LISTING_WAIT_S < ROUTED_TIMEOUT_S


def test_the_executor_waits_longer_only_for_a_call_on_a_node(tmp_path: Path) -> None:
    async def answer(entry: Any, tool: str, args: Any, **kw: Any) -> dict[str, Any]:
        return {}

    specs, *_ = routed_specs(tmp_path, privacy_state(), answer)
    for spec in specs.values():
        assert spec.call_timeout_s is not None
        assert spec.call_timeout_s({"device": LAPTOP}) >= ROUTED_TIMEOUT_S
        assert spec.call_timeout_s({"device": "core"}) is None  # the core's own limit
        assert spec.call_timeout_s({"device": ""}) is None


async def test_precheck_runs_before_around_and_stops_it() -> None:
    from nox.node.router import routed_spec
    from nox.node.tools import NotifyArgs
    from nox.security.model import Risk

    order: list[str] = []
    laptop = registry_entry(LAPTOP, "Laptop", tools={"notify.show": "low"})

    def precheck(payload: dict[str, Any]) -> str | None:
        order.append("precheck")
        return "refused_text" if payload.get("text") == "bad" else None

    async def around(entry: Any, args: dict[str, Any], call: Any) -> dict[str, Any]:
        order.append("around")
        return dict(await call())

    spec = routed_spec(
        name="notify.show",
        description="t",
        risk=Risk.LOW,
        side_effects=True,
        registry=FakeRegistry([laptop]),  # type: ignore[arg-type]
        input_model=NotifyArgs,
        precheck=precheck,
        around=around,
    )
    assert spec.scope is not None
    for text in ("bad", "gut"):
        payload = spec.input_model.model_validate({"text": text, "device": "Laptop"}).model_dump()
        if spec.prepare is not None:
            await spec.prepare(payload)
        spec.scope(payload)
        await spec.handler(payload)
    assert order == ["precheck", "precheck", "around"]


async def test_package_tools_refuse_a_paired_device_instead_of_running_here() -> None:
    from nox.hands.packages import register_package_tools
    from nox.node.router import NOT_AVAILABLE_ON_DEVICE, core_only_spec
    from nox.tools.registry import ToolRegistry

    ran: list[list[str]] = []

    async def run(argv: list[str]) -> tuple[int, str, str]:
        ran.append(argv)
        return 0, "", ""

    tools = ToolRegistry()
    register_package_tools(tools, run)
    own = tools.get("package.install")
    assert own is not None
    entries: list[Any] = [SimpleNamespace(device_id=LAPTOP, linked_device_id=None)]
    registry = SimpleNamespace(
        known=lambda: entries, entries=lambda: entries, core_name=lambda: "Wohnzimmer-Pi"
    )
    spec = core_only_spec(own, registry)  # type: ignore[arg-type]
    schema = spec.input_model.model_json_schema()["properties"]["device"]
    assert "core" in json.dumps(schema)
    payload = spec.input_model.model_validate({"package_id": "VideoLAN.VLC", "device": "Laptop"})
    data = payload.model_dump()
    assert spec.scope is not None
    scope = spec.scope(data)
    assert scope.target == "device:none"
    answer = await spec.handler(data)
    assert answer["error"] == NOT_AVAILABLE_ON_DEVICE and ran == []
    here = spec.input_model.model_validate({"package_id": "VideoLAN.VLC"}).model_dump()
    scope = spec.scope(here)
    assert scope.device_name == "Wohnzimmer-Pi" and scope.target == "VideoLAN.VLC"
    await spec.handler(here)
    assert len(ran) == 1
