"""The text checks of a write are bounded and never in the way of a stop: structured text
is capped and refused before anyone is asked, the scans are linear, they run off the
event loop with a time limit, and a check that runs out of time allows nothing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.node.device.gate import NodeGate
from nox.node.device.hands.args import WriteArgs
from nox.node.device.hands.catalogue import hand_tools
from nox.node.device.hands.node_hands import NodeHands
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.node.wire.protocol import NoxCallMeta
from nox.security.model import Risk
from tests.unit.node.support import (
    Confirmer,
    FakeRegistry,
    call,
    registry_entry,
    session_of,
    signed,
)
from tests.unit.node.support_hands import FakeSandbox, start_hands_node, write_file, write_refusal

# ---- structured text: bounded, and refused before anyone is asked -------------------------------


async def test_deeply_nested_or_huge_structured_text_is_refused(
    empty_fs_world: dict[str, Path],
) -> None:
    assert await write_refusal(empty_fs_world, "notes/tief.json", "[" * 60_000) == "invalid_content"
    big = '{"a": "' + "x" * (300 * 1024) + '"}'
    assert await write_refusal(empty_fs_world, "notes/gross.json", big) == "too_large"


def test_the_node_refuses_bad_structured_text_before_the_dialog_and_the_budget(
    tmp_path: Path,
) -> None:
    from nox.node.wire.receipts import ReceiptSigner, ReceiptVerifier

    signer = ReceiptSigner(tmp_path / "r")
    verifier = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    tools = hand_tools(NodeHands(folders=lambda: [], protected=(tmp_path,), sandbox=lambda: None))
    manifest = NodeManifest(groups={g: g == "files" for g in GROUPS}, max_calls_per_hour=1)
    gate = NodeGate(tools, manifest, None, receipts=verifier)
    for path, text in (("a.json", "{kaputt"), ("a.md", "<% x %>"), ("b.json", "[1]")):
        args = {"path": path, "text": text, "append": False, "overwrite": False}
        meta = signed(signer, "file.write", args, risk="high", device_id="d" * 32, core_id="c" * 64)
        decision = gate.check("file.write", args, meta=NoxCallMeta(**meta))
        if path == "b.json":
            assert decision.allowed  # the one budgeted call is still there
        else:
            assert not decision.allowed and decision.reason in (
                "invalid_content",
                "content_refused",
            )
            assert not decision.needs_local_confirmation


async def test_the_core_refuses_bad_structured_text_before_asking_anyone() -> None:
    from nox.node.hub.install.routes import write_precheck, write_target
    from nox.node.hub.router import routed_spec

    laptop = registry_entry("a" * 32, "Laptop", tools={"file.write": "medium"})
    registry = FakeRegistry([laptop])
    spec = routed_spec(
        name="file.write",
        description="t",
        risk=Risk.MEDIUM,
        side_effects=True,
        registry=registry,  # type: ignore[arg-type]
        input_model=WriteArgs,
        node_target=write_target,
        precheck=write_precheck,
    )
    payload = spec.input_model.model_validate(
        {"path": "plan.yml", "text": "a: !!python/name:os.system", "device": "Laptop"}
    ).model_dump()
    assert spec.scope is not None and spec.prepare is not None
    await spec.prepare(payload)  # the check runs here, off the event loop
    scope = spec.scope(payload)
    assert scope.risk is Risk.READ and scope.target == "device:none"  # nobody is asked
    answer = await spec.handler(payload)
    assert answer["error"] == "content_refused" and registry.calls == []


# ---- linear, bounded, off the event loop ---------------------------------------------------------


ADVERSARIAL = {
    "tags": "<a" * 500_000,
    "tag_then_text": "<a " + "o" * 1_000_000,
    "handlers_without_equals": "<a on" * 200_000,
    "backticks": "`" * 1_000_000,
    "tildes": "~" * 1_000_000,
    "fence_runs": "``` " * 250_000,
    "mixed_runs": "`~" * 500_000,
    "space_before_inline": "`" + " " * 1_000_000 + "$=",
    "long_info_words": ("```" + "x" * 70 + "\n") * 14_000,
    "entities": "&lt;a" * 200_000,
    "tag_without_end": "<a " + "x" * 1_000_000,
    "handler_word_without_equals": "<a on" + "x" * 1_000_000,
    "handler_then_spaces": "<a onx" + " " * 1_000_000,
    "many_handler_words": "<a " + "onx " * 250_000,
    "backtick_then_spaces": "`" + " " * 1_000_000 + "x",
    "fence_then_spaces": "```" + " " * 1_000_000 + "x",
    "fence_then_word": "```" + "x" * 1_000_000,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_the_markdown_scan_is_linear(name: str) -> None:
    import time

    from nox.node.device.safe_fs.text_checks import markdown_runs_code

    started = time.perf_counter()
    markdown_runs_code(ADVERSARIAL[name])
    assert time.perf_counter() - started < 0.5, name


@pytest.mark.parametrize("path", ["notes/lang.md", "notes/lang.txt", "notes/lang.csv"])
async def test_text_over_the_cap_is_refused(empty_fs_world: dict[str, Path], path: str) -> None:
    from nox.node.device.safe_fs.text_checks import MAX_TEXT_BYTES

    assert await write_refusal(empty_fs_world, path, "a" * (MAX_TEXT_BYTES + 1)) == "too_large"
    assert (await write_file(empty_fs_world, path, "a" * 1000))["ok"] is True


async def test_the_core_runs_the_text_check_in_a_thread_with_a_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    from nox.node.hub.install.routes import write_target
    from nox.node.hub.router import routed_spec
    from nox.node.wire import offload as offload_module

    seen: list[str] = []

    def slow(_payload: dict[str, Any]) -> str | None:
        seen.append(threading.current_thread().name)
        time.sleep(0.3)
        return None

    monkeypatch.setattr(offload_module, "CHECK_TIMEOUT_S", 0.05)
    laptop = registry_entry("a" * 32, "Laptop", tools={"file.write": "medium"})
    spec = routed_spec(
        name="file.write",
        description="t",
        risk=Risk.MEDIUM,
        side_effects=True,
        registry=FakeRegistry([laptop]),  # type: ignore[arg-type]
        input_model=WriteArgs,
        node_target=write_target,
        precheck=slow,
    )
    payload = spec.input_model.model_validate(
        {"path": "a.txt", "text": "x", "device": "Laptop"}
    ).model_dump()
    assert spec.prepare is not None and spec.scope is not None
    await spec.prepare(payload)
    assert seen and seen[0] != threading.main_thread().name  # never on the event loop
    assert spec.scope(payload).target == "device:none"
    assert (await spec.handler(payload))["error"] == "check_timeout"


def test_the_node_gate_is_safe_to_run_in_a_thread(tmp_path: Path) -> None:
    import threading

    from nox.node.wire.receipts import ReceiptSigner, ReceiptVerifier

    signer = ReceiptSigner(tmp_path / "r")
    verifier = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    tools = hand_tools(NodeHands(folders=lambda: [], protected=(tmp_path,), sandbox=lambda: None))
    manifest = NodeManifest(groups={g: g == "files" for g in GROUPS}, max_calls_per_hour=5)
    gate = NodeGate(tools, manifest, None, receipts=verifier)
    allowed: list[bool] = []

    def one(i: int) -> None:
        args = {"path": f"a{i}.txt", "text": "x", "append": False, "overwrite": False}
        meta = signed(
            signer, "file.write", args, risk="medium", device_id="d" * 32, core_id="c" * 64
        )
        allowed.append(gate.check("file.write", args, meta=NoxCallMeta(**meta)).allowed)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert allowed.count(True) == 5  # the budget holds under concurrency


# ---- an abandoned check changes nothing ----------------------------------------------------------


async def test_a_slow_text_check_never_holds_up_the_stop(
    listener: Any,
    pairing: Any,
    tls: Any,
    tmp_path: Path,
    empty_fs_world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time

    from nox.node.device.hands import catalogue as catalogue_module
    from nox.node.device.hands import files as files_module

    def slow(path: str, text: str, *, append: bool) -> str | None:
        time.sleep(1.0)  # a worker thread: the event loop goes on
        return None

    monkeypatch.setattr(catalogue_module, "write_text_refusal", slow)
    monkeypatch.setattr(files_module, "write_text_refusal", slow)
    agent, task, stop, _audit = await start_hands_node(
        listener, pairing, tls, tmp_path, empty_fs_world, FakeSandbox(), Confirmer("allow")
    )
    try:
        session = await session_of(listener, agent._home.load_identity().node_id)  # type: ignore[union-attr]
        running = asyncio.create_task(
            call(
                session,
                listener.signer,
                "file.write",
                {"path": "a.txt", "text": "x"},
                risk="medium",
            )
        )
        await asyncio.sleep(0.2)
        started = time.perf_counter()
        await listener.broadcast_kill("test")
        answer = await asyncio.wait_for(running, 5)
        assert time.perf_counter() - started < 0.6  # handled while the check still sleeps
        assert answer["_nox"]["refused"] == "stopped"
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_a_check_that_runs_out_of_time_spends_nothing_and_allows_nothing(
    listener: Any,
    pairing: Any,
    tls: Any,
    tmp_path: Path,
    empty_fs_world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time

    from nox.node.device.hands import catalogue as catalogue_module
    from nox.node.device.hands import files as files_module
    from nox.node.wire import offload as offload_module

    def slow(path: str, text: str, *, append: bool) -> str | None:
        time.sleep(0.4)
        return None

    monkeypatch.setattr(catalogue_module, "write_text_refusal", slow)
    monkeypatch.setattr(files_module, "write_text_refusal", slow)
    monkeypatch.setattr(offload_module, "CHECK_TIMEOUT_S", 0.1)
    agent, task, stop, audit = await start_hands_node(
        listener, pairing, tls, tmp_path, empty_fs_world, FakeSandbox(), Confirmer("allow")
    )
    try:
        session = await session_of(listener, agent._home.load_identity().node_id)  # type: ignore[union-attr]
        answer = await call(
            session,
            listener.signer,
            "file.write",
            {"path": "a.txt", "text": "x"},
            risk="medium",
            call_id="late1",
        )
        assert answer["_nox"]["refused"] == "check_timeout"
        await asyncio.sleep(0.6)  # the abandoned check has finished by now
        gate = agent._gate
        assert len(gate._side_effects) == 0  # no budget spent
        assert gate.receipts is not None and "late1" not in gate.receipts._used  # receipt unused
        rows = [e for e in audit.entries(since_seq=0, limit=500) if e.target == "late1"]
        assert [(e.action, e.decision) for e in rows] == [("node.refused", "deny")]
        assert not (empty_fs_world["root"] / "a.txt").exists()
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


async def test_structured_text_is_capped_lower_than_plain_text(
    empty_fs_world: dict[str, Path],
) -> None:
    from nox.node.device.safe_fs.formats import MAX_STRUCTURED_BYTES

    big = '{"a": "' + "x" * MAX_STRUCTURED_BYTES + '"}'
    assert await write_refusal(empty_fs_world, "notes/gross.json", big) == "too_large"
    assert (await write_file(empty_fs_world, "notes/gross.md", "x" * (MAX_STRUCTURED_BYTES + 10)))[
        "ok"
    ]
