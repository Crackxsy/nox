"""The question on the device: nothing high runs without it, it shows everything or nothing,
a broken dialog or a reflex "yes" is a no, nothing with a side effect runs while a
question is open, and the dialog says in one plain sentence what is asked."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from nox.lan.tls import TlsMaterial
from nox.node import agent as agent_module
from nox.node.agent import confirmation_text
from nox.node.home import NodeHome
from nox.node.listener import NodeListener
from nox.node.manifest import NodeManifest
from nox.node.pairing import NodePairing
from nox.node.tools import NodeTool
from tests.unit.node.support import (
    Confirmer,
    TargetArgs,
    call,
    call_meta,
    gate_with_every_group,
    session_of,
    start_node,
)


@pytest.mark.parametrize(
    ("tool_risk", "meta_risk", "delegated", "available", "answer", "trusted"),
    list(
        itertools.product(
            ("low", "high"),
            ("low", "high"),
            (False, True),
            (False, True),
            ("allow", "deny", "timeout"),
            (False, True),
        )
    ),
)
async def test_nothing_high_ever_runs_without_a_confirmation(
    listener: NodeListener,
    pairing: NodePairing,
    tls: TlsMaterial,
    tmp_path: Path,
    tool_risk: str,
    meta_risk: str,
    delegated: bool,
    available: bool,
    answer: str,
    trusted: bool,
) -> None:
    """The matrix of the review's HIGH finding: core risk x node risk x delegation x a confirmer
    on the device x its answer x `trust_core_for_high`."""
    if _rank(meta_risk) < _rank(tool_risk):
        pytest.skip("understating the risk is refused before anything else (tested above)")
    confirmer = Confirmer(answer, available=available)
    node = await start_node(
        listener,
        pairing,
        tls,
        tmp_path,
        confirmer=confirmer,
        manifest=NodeManifest(trust_core_for_high=trusted),
    )
    try:
        session = await session_of(listener, node.node_id)
        name = "system.high" if tool_risk == "high" else "system.low"
        result = await call(
            session,
            listener.signer,
            name,
            {"target": "Arbeits-PC"},  # type: ignore[attr-defined]
            risk=meta_risk,
            confirm_on_device=delegated,
        )
        ran = bool(node.ran)
        needs = tool_risk == "high" or meta_risk == "high" or delegated
        if not needs:
            assert ran and not confirmer.asked
        elif available:
            assert ran == (answer == "allow"), result
            assert confirmer.asked  # the person was asked
        else:
            # Headless: only a core that confirmed itself, and only when trusted locally. A call
            # the core left to the device was confirmed by nobody - always refused.
            assert ran == (trusted and not delegated), result  # decided_by "user" here
            if not ran:
                assert result["_nox"]["refused"] == "high_needs_local_confirmation"
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


def _rank(risk: str) -> int:
    return ("read", "low", "medium", "high", "critical").index(risk)


async def test_the_node_says_whether_it_can_ask(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    for available in (True, False):
        node = await start_node(
            listener, pairing, tls, tmp_path, confirmer=Confirmer(available=available)
        )
        try:
            session = await session_of(listener, node.node_id)
            assert session.hello.confirmer is available
        finally:
            node.stop.set()
            await asyncio.wait_for(node.task, 5)


@pytest.mark.parametrize(
    "value",
    [
        "<b>klick</b> &amp; <a href='x'>hier</a>",  # markup is text
        "C:\\Users\\Beispiel\\Downloads\\" + "x" * 300,  # a long path, in full
        "rm\x1b[2J\r\nAllow: yes\u202e",  # control and bidi characters
    ],
)
def test_the_question_shows_everything_or_nothing(value: str) -> None:
    from nox.node.protocol import render

    text = confirmation_text("package.install", {"name": value})
    assert text is not None
    assert "\x1b" not in text and "\r" not in text and "\u202e" not in text
    assert render(value) in text  # in full, rendered as the core renders it - nothing cut
    assert confirmation_text("package.install", {"name": "y" * 5000}) is None  # too long: refuse


async def test_too_long_to_confirm_is_refused_not_cut(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    confirmer = Confirmer("allow")
    node = await start_node(listener, pairing, tls, tmp_path, confirmer=confirmer)
    try:
        session = await session_of(listener, node.node_id)
        result = await call(
            session,
            listener.signer,
            "system.high",
            {"target": "z" * 5000},
            risk="high",  # type: ignore[attr-defined]
        )
        assert result["_nox"]["refused"] == "cannot_show_in_full"
        assert not confirmer.asked and not node.ran
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


async def test_a_broken_dialog_is_a_no(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    class Broken(Confirmer):
        async def ask(self, text: str, *, timeout_s: int = 60) -> str:
            raise RuntimeError("no display")

    node = await start_node(listener, pairing, tls, tmp_path, confirmer=Broken())
    try:
        session = await session_of(listener, node.node_id)
        result = await call(session, listener.signer, "system.high", risk="high")  # type: ignore[attr-defined]
        assert result["_nox"]["refused"] == "denied_on_device" and not node.ran
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


# ---- M-c: nothing types while the person is asked; a reflex "yes" is a no ----


def test_nothing_with_a_side_effect_runs_while_a_dialog_is_open(tmp_path: Path) -> None:
    gate, signer = gate_with_every_group(tmp_path)
    gate.dialog_open = lambda: True
    gate.input_lock = lambda: None  # no game here (the game lock is tested in test_c6)
    args = {"text": "y"}
    for name in ("desktop.type_text", "system.low", "notify.show"):
        meta = call_meta(signer, name, args)
        assert gate.check(name, args, meta=meta).reason == "dialog_open", name
    assert gate.check("system.info", {}).allowed  # reading is unaffected


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


def test_trust_core_for_high_is_set_with_nox_node_allow(tmp_path: Path) -> None:
    """N3: reachable through the confirmed path, not only by editing the file."""
    from nox.node import cli as node_cli

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


def test_the_dialog_says_when_foreign_content_was_involved() -> None:
    """N6."""
    plain = confirmation_text("package.install", {"name": "VLC"}, lang="de")
    warned = confirmation_text("package.install", {"name": "VLC"}, tainted=True, lang="de")
    assert plain is not None and warned is not None
    assert "fremden Inhalten" in warned and "fremden Inhalten" not in plain


async def test_running_input_calls_stop_before_a_question_appears(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:

    node = await start_node(listener, pairing, tls, tmp_path, confirmer=Confirmer("deny"))
    typed: list[str] = []

    async def typing(_a: BaseModel) -> dict[str, Any]:
        typed.append("start")
        await asyncio.sleep(30)
        typed.append("end")
        return {}

    node.agent._gate._tools["desktop.type_text"] = NodeTool(
        "desktop.type_text", "desktop", "low", "t", TargetArgs, typing, path_safe=True
    )
    node.agent._gate._manifest.groups["desktop"] = True
    node.agent._gate.input_lock = lambda: None  # no game here (the game lock: test_c6)
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


async def test_every_running_side_effect_stops_before_a_question(
    listener: NodeListener, pairing: NodePairing, tls: TlsMaterial, tmp_path: Path
) -> None:
    """Not only typing: a plain low-risk call that is still running is stopped too."""
    node = await start_node(listener, pairing, tls, tmp_path, confirmer=Confirmer("deny"))
    try:
        session = await session_of(listener, node.node_id)
        waiting = asyncio.create_task(call(session, listener.signer, "system.wait", timeout=30))  # type: ignore[attr-defined]
        for _ in range(100):
            if "wait-start" in node.ran:
                break
            await asyncio.sleep(0.02)
        await call(session, listener.signer, "system.high", risk="high")  # type: ignore[attr-defined]
        result = await asyncio.wait_for(waiting, 5)
        assert result["_nox"]["refused"] == "dialog_open" and "wait-end" not in node.ran
    finally:
        node.stop.set()
        await asyncio.wait_for(node.task, 5)


# ---- M3: the device's dialog ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "args", "de", "en"),
    [
        ("screen.capture", {}, "Bildschirmfoto", "screenshot"),
        ("camera.snapshot", {}, "Kamera", "camera"),
        ("computer.session_start", {"purpose": "Ordnen", "minutes": 7}, "bis 7 min", "up to 7 min"),
        ("shell.run", {"argv": ["ls"]}, "Befehl", "command"),
    ],
)
def test_the_dialog_opens_with_one_plain_sentence(
    tool: str, args: dict[str, Any], de: str, en: str
) -> None:
    from nox.node.agent import confirmation_text

    german = confirmation_text(tool, args, lang="de")
    english = confirmation_text(tool, args, lang="en")
    assert german is not None and english is not None
    assert de in german.splitlines()[0] and en in english.splitlines()[0]
    assert tool in german and german.endswith("Ablehnen)") and english.endswith("deny)")


def test_the_session_dialog_says_what_a_session_means() -> None:
    from nox.node.agent import confirmation_text

    text = confirmation_text("computer.session_start", {"purpose": "Fenster ordnen"}, lang="de")
    assert text is not None and text.startswith(
        "Nox steuert bis 15 min Maus und Tastatur und kann in dieser Zeit alles, was du an "
        "diesem PC kannst."
    )
