"""What the gate does with the strings the core sends: nothing is resolved, expanded or
opened before the receipt is checked, path aliases and invalid Unicode are refused on the
text alone, the node folder is closed to every tool, and the text that was signed is the
text that is shown and run."""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from nox.lan.tls import TlsMaterial
from nox.node.device.agent import confirmation_text
from nox.node.device.gate import NodeGate, inside
from nox.node.device.tools import NodeTool, default_tools
from nox.node.hub.listener import NodeListener
from nox.node.hub.pairing import NodePairing
from nox.node.wire.manifest import GROUPS, NodeManifest
from nox.node.wire.protocol import NoxCallMeta
from tests.unit.node.support import (
    CORE_ID,
    DEVICE_ID,
    call,
    call_meta,
    gate_with_every_group,
    node_home,
    session_of,
    signed,
    start_node,
)

# ---- nothing the core sends is resolved, expanded or opened --------------------------------------


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
    """`Path(word).resolve()` on `\\\\attacker\\x` made Windows open an SMB
    connection (NetNTLM hash leak) and blocked the event loop. The gate compares text only."""
    gate, signer = gate_with_every_group(tmp_path)
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
    args = {"path": text}
    decision = gate.check("system.low", args, meta=call_meta(signer, "system.low", args))
    assert decision.reason == "path_alias_refused", decision.reason  # refused on the text alone
    # Free text (a notice, a note) is not a path: allowed - and never touched.
    words = {"text": text}
    assert gate.check("notify.show", words, meta=call_meta(signer, "notify.show", words)).allowed
    assert not any("attacker" in t or "geheim" in t for t in touched), touched


def test_the_receipt_is_checked_before_any_argument(tmp_path: Path) -> None:
    gate, signer = gate_with_every_group(tmp_path)
    home = str(tmp_path / "node-home" / "node.key")
    unsigned = NoxCallMeta(call_id="c1", risk="low", deadline=time.time() + 30)
    # Without a valid receipt the arguments are never looked at - not even for the folder check.
    assert gate.check("system.low", {"text": home}, meta=unsigned).reason == "receipt_missing"
    valid = call_meta(signer, "system.low", {"text": home})
    assert gate.check("system.low", {"text": home}, meta=valid).reason == "node_home_protected"


def test_remote_and_device_paths_count_as_inside_without_being_touched(tmp_path: Path) -> None:
    for path in ("\\\\attacker\\x", "//attacker/x", "\\\\?\\C:\\x", "\\\\.\\COM1"):
        assert inside(path, tmp_path) is True


def test_invalid_unicode_is_refused_and_nfc_is_one_text(tmp_path: Path) -> None:
    gate, signer = gate_with_every_group(tmp_path)
    composed = unicodedata.normalize("NFC", "Wäsche")
    decomposed = unicodedata.normalize("NFD", "Wäsche")
    assert composed != decomposed
    meta = call_meta(signer, "system.low", {"text": composed})
    assert gate.check("system.low", {"text": decomposed}, meta=meta).allowed  # one text
    broken = {"text": "x\ud800y"}
    with pytest.raises(ValueError):
        signed(signer, "system.low", broken, device_id=DEVICE_ID, core_id=CORE_ID)
    fake = NoxCallMeta(
        call_id="c9", risk="low", deadline=time.time() + 30, receipt="AAAA", issued_at=time.time()
    )
    assert gate.check("system.low", broken, meta=fake).reason == "receipt_invalid"
    assert confirmation_text("system.high", broken) is None


# ---- path aliases, and tools that never said how they treat paths --------------------------------


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
    gate, signer = gate_with_every_group(tmp_path)
    args = {"path": text}
    assert gate.check("system.low", args, meta=call_meta(signer, "system.low", args)).reason in (
        "path_alias_refused",
        "node_home_protected",
    )


@pytest.mark.parametrize("text", ["Wäsche aufhängen", "Warte...", "um ~5 Uhr", "a/b.txt"])
def test_ordinary_text_is_no_alias(tmp_path: Path, text: str) -> None:
    gate, signer = gate_with_every_group(tmp_path)
    args = {"text": text}
    assert gate.check("system.low", args, meta=call_meta(signer, "system.low", args)).allowed


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


@pytest.mark.parametrize(
    "where",
    ["manifest.key", "node.yaml", "audit.db", "node.key", "", "sub/../node.key"],
)
def test_no_node_tool_may_touch_the_node_folder(tmp_path: Path, where: str) -> None:
    from pydantic import BaseModel, ConfigDict

    from nox.node.device.gate import NodeGate, protected_forms, touches
    from nox.node.device.tools import NodeTool
    from nox.node.wire.manifest import GROUPS, NodeManifest

    home = node_home(tmp_path)

    class FileArgs(BaseModel):
        model_config = ConfigDict(extra="forbid")
        path: str

    async def never(_a: BaseModel) -> dict[str, Any]:
        raise AssertionError("ran")

    tool = NodeTool("file.read", "files", "read", "t", FileArgs, never)
    gate = NodeGate(
        {"file.read": tool},
        NodeManifest(groups={g: True for g in GROUPS}),
        None,
        protected=home.path,
    )
    target = str(home.path / where) if where else str(home.path)
    for variant in (
        target,
        target.upper(),
        target.replace("/", "\\"),
        f'cat "{target}"',
        f"--config={target}",
    ):
        assert touches({"path": variant}, protected_forms(home.path)), variant
        assert gate.check("file.read", {"path": variant}).reason == "node_home_protected"
    assert gate.check("file.read", {"path": str(tmp_path / "elsewhere.txt")}).allowed


def test_a_link_or_short_name_into_the_node_folder_is_still_inside(tmp_path: Path) -> None:
    """Review follow-up: the real path decides - symlinks, junctions and 8.3 names included."""
    import os
    import subprocess
    import sys

    from nox.node.device.gate import inside

    home = node_home(tmp_path)
    secret = home.path / "manifest.key"
    assert secret.exists()
    link = tmp_path / "harmless-looking"
    if sys.platform == "win32":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(home.path)],
            capture_output=True,
            check=False,
        )
        assert made.returncode == 0, made.stderr
    else:
        os.symlink(home.path, link)
    assert inside(link / "manifest.key", home.path)
    if sys.platform == "win32":
        import ctypes

        buffer = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(secret), buffer, 1024):  # type: ignore[attr-defined]
            assert inside(buffer.value, home.path)
    assert not inside(tmp_path / "elsewhere", home.path)
