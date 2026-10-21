"""The external review of C4: every finding has a test here that is red without its fix."""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from nox.lan.tls import TlsMaterial
from nox.node import agent as agent_module
from nox.node.agent import confirmation_text
from nox.node.gate import NodeGate, inside
from nox.node.listener import NodeListener
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.pairing import NodePairing
from nox.node.protocol import NoxCallMeta
from nox.node.receipts import ReceiptSigner, ReceiptVerifier
from nox.node.tools import NodeTool, default_tools
from nox.security.audit import SqliteAuditLog
from tests.unit.node.test_c4 import Confirmer, call, session_of, signed, start_node

DEVICE = "d" * 32
CORE = "c" * 64


def _gate(
    tmp_path: Path, manifest: NodeManifest | None = None, audit: Any = None
) -> tuple[NodeGate, ReceiptSigner]:
    signer = ReceiptSigner(tmp_path / "r")
    home = tmp_path / "node-home"
    home.mkdir(exist_ok=True)

    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str = "x"

    async def run(_a: BaseModel) -> dict[str, Any]:
        return {}

    tools = default_tools(None)
    tools["desktop.type_text"] = NodeTool(
        "desktop.type_text", "desktop", "low", "t", Args, run, path_safe=True
    )
    tools["system.low"] = NodeTool("system.low", "system", "low", "t", Args, run, path_safe=True)
    gate = NodeGate(
        tools,
        manifest or NodeManifest(groups={g: True for g in GROUPS}),
        audit,
        protected=home,
    )
    gate.receipts = ReceiptVerifier(signer.public_key, device_id=DEVICE, core_id=CORE)
    return gate, signer


def _meta(signer: ReceiptSigner, name: str, args: Any, **kw: Any) -> NoxCallMeta:
    return NoxCallMeta.model_validate(
        signed(signer, name, args, device_id=DEVICE, core_id=CORE, **kw)
    )


# ---- H1: nothing the core sends is resolved, expanded or opened ---------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "\\\\attacker\\share\\x",
        "//attacker/share/x",
        "\\\\?\\UNC\\attacker\\share",
        "\\\\.\\pipe\\attacker",
        "Hallo \\\\attacker\\share und ~/geheim",
    ],
)
def test_core_strings_never_reach_the_file_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    """Review H1: `Path(word).resolve()` on `\\\\attacker\\x` made Windows open an SMB
    connection (NetNTLM hash leak) and blocked the event loop. The gate compares text only."""
    gate, signer = _gate(tmp_path)
    touched: list[str] = []
    real_resolve = pathlib.Path.resolve

    def spy_resolve(self: pathlib.Path, *a: Any, **k: Any) -> pathlib.Path:
        touched.append(str(self))
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "resolve", spy_resolve)
    monkeypatch.setattr(pathlib.Path, "expanduser", lambda self: touched.append(str(self)) or self)
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: touched.append(str(p)) or p)
    monkeypatch.setattr(
        os, "stat", lambda p, *a, **k: touched.append(str(p)) or (_ for _ in ()).throw(OSError())
    )
    args = {"text": text}
    decision = gate.check("notify.show", args, meta=_meta(signer, "notify.show", args))
    assert decision.reason == "path_alias_refused", decision.reason  # refused on the text alone
    assert not any("attacker" in t or "geheim" in t for t in touched), touched


def test_the_receipt_is_checked_before_any_argument(tmp_path: Path) -> None:
    gate, signer = _gate(tmp_path)
    home = str(tmp_path / "node-home" / "node.key")
    unsigned = NoxCallMeta(call_id="c1", risk="low", deadline=time.time() + 30)
    # Without a valid receipt the arguments are never looked at - not even for the folder check.
    assert gate.check("system.low", {"text": home}, meta=unsigned).reason == "receipt_missing"
    valid = _meta(signer, "system.low", {"text": home})
    assert gate.check("system.low", {"text": home}, meta=valid).reason == "node_home_protected"


def test_remote_and_device_paths_count_as_inside_without_being_touched(tmp_path: Path) -> None:
    for path in ("\\\\attacker\\x", "//attacker/x", "\\\\?\\C:\\x", "\\\\.\\COM1"):
        assert inside(path, tmp_path) is True


# ---- M-a: a dead-man stop lifts when the core is back; the core sees the state ----


async def test_the_dead_man_stop_lifts_after_pings_and_the_core_sees_it(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    node = await start_node(listener, pairing, tls, tmp_path)
    try:
        session = await session_of(listener, node.node_id)
        node.agent.dead_man = True
        for seq in range(1, agent_module.DEAD_MAN_RESET_PINGS + 1):
            await session.client.notify("nox/ping", {"seq": seq, "ts": time.time()})
            for _ in range(100):
                if session.node_state != "" or node.agent.dead_man is False:
                    break
                await asyncio.sleep(0.01)
            if seq == 1:
                assert session.node_state == "dead_man"  # reported to the core
        for _ in range(100):
            if not node.agent.dead_man:
                break
            await asyncio.sleep(0.01)
        assert node.agent.dead_man is False and node.agent.stopped_reason() is None
        # While the core itself is stopped, pings do not lift it.
        node.agent.dead_man, node.agent.core_killed = True, True
        for seq in range(10, 14):
            await session.client.notify("nox/ping", {"seq": seq, "ts": time.time()})
        await asyncio.sleep(0.2)
        assert node.agent.dead_man is True
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


# ---- M-c: nothing types while the person is asked; a reflex "yes" is a no ----


def test_input_tools_are_refused_while_a_dialog_is_open(tmp_path: Path) -> None:
    gate, signer = _gate(tmp_path)
    gate.dialog_open = lambda: True
    args = {"text": "y"}
    meta = _meta(signer, "desktop.type_text", args)
    assert gate.check("desktop.type_text", args, meta=meta).reason == "dialog_open"
    assert gate.check("system.info", {}).allowed  # anything else is unaffected


async def test_a_yes_faster_than_reading_is_a_no(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_module, "MIN_DECISION_S", 1.5)
    node = await start_node(listener, pairing, tls, tmp_path, confirmer=Confirmer("allow"))
    try:
        session = await session_of(listener, node.node_id)
        result = await call(session, listener.signer, "system.high", risk="high")  # type: ignore[attr-defined]
        assert result["_nox"]["refused"] == "denied_on_device" and not node.ran
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


# ---- M-d: the Linux dialog is zenity only, default "No", no markup ----


async def test_only_zenity_and_always_no_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from nox.node import confirm as confirm_module

    monkeypatch.setattr(confirm_module.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        confirm_module.shutil,
        "which",
        lambda name: "/usr/bin/kdialog" if name == "kdialog" else None,
    )
    assert confirm_module.PlatformConfirmer().available is False  # kdialog alone is not enough
    monkeypatch.setattr(confirm_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    seen: list[list[str]] = []

    class Proc:
        async def wait(self) -> int:
            return 1

    async def fake_exec(*argv: str, **_kw: Any) -> Proc:
        seen.append(list(argv))
        return Proc()

    monkeypatch.setattr(confirm_module.asyncio, "create_subprocess_exec", fake_exec)
    assert await confirm_module.PlatformConfirmer().ask("<b>x</b>") == "deny"
    assert seen[0][0] == "zenity" and "--default-cancel" in seen[0] and "--no-markup" in seen[0]


# ---- LOW ---------------------------------------------------------------------------------------


def test_a_restart_does_not_refill_the_budget(tmp_path: Path) -> None:
    """N1, and the budget charges the higher of the node's and the core's risk."""
    audit = SqliteAuditLog(sqlite3.connect(tmp_path / "audit.db", check_same_thread=False))
    manifest = NodeManifest(
        groups={g: True for g in GROUPS}, max_calls_per_hour=3, max_high_per_hour=1
    )
    gate, signer = _gate(tmp_path, manifest, audit)
    args = {"text": "y"}
    # A low tool the core calls high counts against the high budget.
    assert gate.check(
        "system.low", args, meta=_meta(signer, "system.low", args, risk="high")
    ).allowed
    assert gate.check("system.low", args, meta=_meta(signer, "system.low", args)).allowed
    restarted, _ = _gate(tmp_path, manifest, audit)
    restarted.receipts = gate.receipts
    restarted.restore_budget(audit)
    assert (
        restarted.check(
            "system.low", args, meta=_meta(signer, "system.low", args, risk="high")
        ).reason
        == "budget"
    )  # the one high call of the hour was already spent
    assert restarted.check("system.low", args, meta=_meta(signer, "system.low", args)).allowed
    assert (
        restarted.check("system.low", args, meta=_meta(signer, "system.low", args)).reason
        == "budget"
    )


def test_a_receipt_for_another_node_or_core_is_invalid(tmp_path: Path) -> None:
    """N2."""
    gate, signer = _gate(tmp_path)
    args = {"text": "y"}
    other_node = NoxCallMeta.model_validate(
        signed(signer, "system.low", args, device_id="e" * 32, core_id=CORE)
    )
    assert gate.check("system.low", args, meta=other_node).reason == "receipt_invalid"
    other_core = NoxCallMeta.model_validate(
        signed(signer, "system.low", args, device_id=DEVICE, core_id="f" * 64)
    )
    assert gate.check("system.low", args, meta=other_core).reason == "receipt_invalid"


def test_trust_core_for_high_is_set_with_nox_node_allow(tmp_path: Path) -> None:
    """N3: reachable through the confirmed path, not only by editing the file."""
    from nox.node import cli as node_cli
    from nox.node.home import NodeHome

    home = NodeHome(tmp_path / "h")
    home.ensure()
    node_cli.confirm_defaults(home, None)
    node_cli.allow(
        home,
        "trust_core_for_high",
        True,
        say=lambda _t: None,
        interactive=lambda: True,
        confirm=lambda _p: True,
    )
    assert node_cli.build(home).gate.manifest.trust_core_for_high is True
    node_cli.allow(home, "trust_core_for_high", False, say=lambda _t: None)
    assert node_cli.build(home).gate.manifest.trust_core_for_high is False


def test_invalid_unicode_is_refused_and_nfc_is_one_text(tmp_path: Path) -> None:
    """N4."""
    gate, signer = _gate(tmp_path)
    composed = unicodedata.normalize("NFC", "Wäsche")
    decomposed = unicodedata.normalize("NFD", "Wäsche")
    assert composed != decomposed
    meta = _meta(signer, "system.low", {"text": composed})
    assert gate.check("system.low", {"text": decomposed}, meta=meta).allowed  # one text
    broken = {"text": "x\ud800y"}
    with pytest.raises(ValueError):
        signed(signer, "system.low", broken, device_id=DEVICE, core_id=CORE)
    fake = NoxCallMeta(
        call_id="c9", risk="low", deadline=time.time() + 30, receipt="AAAA", issued_at=time.time()
    )
    assert gate.check("system.low", broken, meta=fake).reason == "receipt_invalid"
    assert confirmation_text("system.high", broken) is None


def test_the_dialog_says_when_foreign_content_was_involved() -> None:
    """N6."""
    plain = confirmation_text("package.install", {"name": "VLC"})
    warned = confirmation_text("package.install", {"name": "VLC"}, tainted=True)
    assert plain is not None and warned is not None
    assert "fremden Inhalten" in warned and "fremden Inhalten" not in plain


_ = json


# ---- follow-ups ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "~/.local/share/nox/node/node.key",
        "~",
        "C:/Users/Beispiel/AppData/Local/Temp/../../Local/Nox/node/node.key",
        r"..\node.key",
        "C:/Users/BEISPI~1/AppData/Local/Nox/NOXNOD~1/node.key",
        "NOXNOD~1",
        r"\\?\C:\Users\Beispiel",
        r"\\.\PhysicalDrive0",
        r"\\server\share",
    ],
)
def test_path_aliases_are_refused_on_the_text_alone(tmp_path: Path, text: str) -> None:
    gate, signer = _gate(tmp_path)
    args = {"text": text}
    assert gate.check("system.low", args, meta=_meta(signer, "system.low", args)).reason in (
        "path_alias_refused",
        "node_home_protected",
    )


@pytest.mark.parametrize("text", ["Wäsche aufhängen", "Warte...", "um ~5 Uhr", "a/b.txt"])
def test_ordinary_text_is_no_alias(tmp_path: Path, text: str) -> None:
    gate, signer = _gate(tmp_path)
    args = {"text": text}
    assert gate.check("system.low", args, meta=_meta(signer, "system.low", args)).allowed


def test_a_side_effect_tool_without_a_path_declaration_is_not_offered(tmp_path: Path) -> None:
    class Args(BaseModel):
        model_config = ConfigDict(extra="forbid")

    async def run(_a: BaseModel) -> dict[str, Any]:
        return {}

    tools = {
        "file.write": NodeTool("file.write", "files", "medium", "t", Args, run),
        "file.read": NodeTool("file.read", "files", "read", "t", Args, run),
    }
    gate = NodeGate(tools, NodeManifest(groups={g: True for g in GROUPS}), None)
    assert [t.name for t in gate.offered()] == ["file.read"]
    assert gate.check("file.write", {}).reason == "unknown_tool"


async def test_running_input_calls_stop_before_a_question_appears(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    from tests.unit.node.test_c4 import _Args

    node = await start_node(listener, pairing, tls, tmp_path, confirmer=Confirmer("deny"))
    typed: list[str] = []

    async def typing(_a: BaseModel) -> dict[str, Any]:
        typed.append("start")
        await asyncio.sleep(30)
        typed.append("end")
        return {}

    node.agent._gate._tools["desktop.type_text"] = NodeTool(
        "desktop.type_text", "desktop", "low", "t", _Args, typing, path_safe=True
    )
    node.agent._gate._manifest.groups["desktop"] = True
    try:
        session = await session_of(listener, node.node_id)
        typing_call = asyncio.create_task(
            call(session, listener.signer, "desktop.type_text", timeout=30)  # type: ignore[attr-defined]
        )
        for _ in range(100):
            if typed:
                break
            await asyncio.sleep(0.02)
        await call(session, listener.signer, "system.high", risk="high")  # type: ignore[attr-defined]
        result = await asyncio.wait_for(typing_call, 5)
        assert result["_nox"]["refused"] == "dialog_open" and "end" not in typed
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def test_the_same_text_is_signed_shown_and_run(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    """A decomposed umlaut from the core: the handler runs with the NFC text the receipt was
    checked against, never with other bytes."""
    shown: list[str] = []

    class Notifier:
        async def show(self, title: str, text: str) -> None:
            shown.append(text)

    node = await start_node(listener, pairing, tls, tmp_path)
    node.agent._gate._tools["notify.show"] = default_tools(Notifier())["notify.show"]
    try:
        session = await session_of(listener, node.node_id)
        decomposed = unicodedata.normalize("NFD", "Wäsche")
        await call(session, listener.signer, "notify.show", {"text": decomposed})  # type: ignore[attr-defined]
        assert shown == [unicodedata.normalize("NFC", "Wäsche")]
        assert unicodedata.is_normalized("NFC", shown[0])
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)
