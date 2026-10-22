"""C5 follow-up: app workspaces and repositories - also above the allowed folder -, Markdown that a
note app would run, structured text checked before anyone is asked, and exact device ids in
profile rules that allow something on a node."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nox.node.gate import NodeGate
from nox.node.hands import NodeHands, WriteArgs, hand_tools
from nox.node.home import NodeHome
from nox.node.manifest import GROUPS, NodeManifest
from nox.node.protocol import NoxCallMeta
from nox.node.tools import NodeRefusal
from nox.security.model import Risk
from tests.unit.node.test_c4 import signed
from tests.unit.node.test_router import FakeRegistry, _entry


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "allowed"
    (root / "notes").mkdir(parents=True)
    node = tmp_path / "node-home"
    node.mkdir()
    return {"root": root, "node": node}


def _tools(root: Path, node: Path) -> dict[str, Any]:
    hands = NodeHands(folders=lambda: [str(root)], protected=(node,), sandbox=lambda: None)
    return hand_tools(hands)


async def _write(world: dict[str, Path], path: str, text: str = "x", **kw: Any) -> Any:
    tool = _tools(world["root"], world["node"])["file.write"]
    return await tool.handler(WriteArgs(path=path, text=text, **kw))


async def _refused(world: dict[str, Path], path: str, text: str = "x", **kw: Any) -> str:
    with pytest.raises(NodeRefusal) as caught:
        await _write(world, path, text, **kw)
    assert not (world["root"] / path).exists()
    return caught.value.reason


# ---- app workspaces ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker", [".obsidian", ".logseq", "logseq/config.edn", ".foam", ".dendron", ".vscode", ".idea"]
)
async def test_nothing_in_an_app_workspace_is_written(world: dict[str, Path], marker: str) -> None:
    vault = world["root"] / "Tresor"
    (vault / "Tagebuch").mkdir(parents=True)
    target = vault / marker
    if marker.endswith(".edn"):
        target.parent.mkdir()
        target.write_text("{}", encoding="utf-8")
    else:
        target.mkdir()
    for path in ("Tresor/notiz.md", "Tresor/Tagebuch/heute.md"):
        assert await _refused(world, path) == "app_workspace_refused"
    assert (await _write(world, "notes/neben.md"))["ok"] is True  # a plain folder beside it


@pytest.mark.parametrize("marker", [".jj", ".bzr", "_darcs", ".sl", ".git", ".hg", ".svn"])
async def test_every_version_control_marker_counts(world: dict[str, Path], marker: str) -> None:
    (world["root"] / "projekt" / marker).mkdir(parents=True)
    assert await _refused(world, "projekt/liste.txt") == "working_tree_refused"


# ---- above the allowed folder -------------------------------------------------------------------


async def test_a_repository_or_vault_around_the_allowed_folder_takes_no_writes(
    tmp_path: Path,
) -> None:
    for marker, reason in (
        (".git", "working_tree_refused"),
        (".obsidian", "app_workspace_refused"),
    ):
        outer = tmp_path / f"aussen{marker}"
        root = outer / "unterordner" / "erlaubt"
        root.mkdir(parents=True)
        (outer / marker).mkdir()
        world = {"root": root, "node": tmp_path / "node-home"}
        assert await _refused(world, "liste.txt") == reason


def test_allowing_a_folder_inside_a_repository_or_vault_is_refused(tmp_path: Path) -> None:
    from nox.node import cli as node_cli

    home = NodeHome(tmp_path / "h")
    yes = {"interactive": lambda: True, "confirm": lambda _p: True}
    for marker in (".git", ".obsidian"):
        outer = tmp_path / f"aussen{marker}"
        inner = outer / "Dokumente"
        inner.mkdir(parents=True)
        (outer / marker).mkdir()
        for folder in (outer, inner):
            with pytest.raises(node_cli.NodeCliError) as caught:
                node_cli.allow(  # type: ignore[arg-type]
                    home, "files", True, say=lambda _t: None, folder=str(folder), **yes
                )
            assert "Obsidian" in str(caught.value)
    plain = tmp_path / "Nox-Dateien"
    plain.mkdir()
    node_cli.allow(home, "files", True, say=lambda _t: None, folder=str(plain), **yes)  # type: ignore[arg-type]


# ---- Markdown a note app would run -------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "# Plan\n```dataviewjs\ndv.paragraph(require('fs'))\n```\n",
        "Heute: `$= dv.current().file.name`",
        "```dataview\nTABLE x\n```\n`$=1`",
        "```DataView\nTABLE file.name WHERE dv.pages()\n```",
        "Titel: <% tp.file.title %>",
        "<SCRIPT>alert(1)</SCRIPT>",
        "[klick](JavaScript:alert(1))",
    ],
)
async def test_markdown_with_code_for_a_note_app_is_refused(
    world: dict[str, Path], text: str
) -> None:
    assert await _refused(world, "notes/plan.md", text) == "content_refused"


async def test_ordinary_markdown_is_written(world: dict[str, Path]) -> None:
    text = "# Einkauf\n- Tomaten\n- Kaffee\n\n```dataview\nLIST FROM #einkauf\n```\n"
    assert (await _write(world, "notes/einkauf.md", text))["ok"] is True


# ---- structured text: bounded, and refused before anyone is asked -------------------------------


async def test_deeply_nested_or_huge_structured_text_is_refused(world: dict[str, Path]) -> None:
    assert await _refused(world, "notes/tief.json", "[" * 200_000) == "invalid_content"
    big = '{"a": "' + "x" * (300 * 1024) + '"}'
    assert await _refused(world, "notes/gross.json", big) == "too_large"


def test_the_node_refuses_bad_structured_text_before_the_dialog_and_the_budget(
    tmp_path: Path,
) -> None:
    from nox.node.receipts import ReceiptSigner, ReceiptVerifier

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
    from nox.node.install import _write_target
    from nox.node.router import routed_spec

    laptop = _entry("a" * 32, "Laptop", tools={"file.write": "medium"})
    registry = FakeRegistry([laptop])
    spec = routed_spec(
        name="file.write",
        description="t",
        risk=Risk.MEDIUM,
        side_effects=True,
        registry=registry,  # type: ignore[arg-type]
        input_model=WriteArgs,
        node_target=_write_target,
    )
    payload = spec.input_model.model_validate(
        {"path": "plan.yml", "text": "a: !!python/name:os.system", "device": "Laptop"}
    ).model_dump()
    assert spec.scope is not None
    scope = spec.scope(payload)
    assert scope.risk is Risk.READ and scope.target == "device:none"  # nobody is asked
    answer = await spec.handler(payload)
    assert answer["error"] == "content_refused" and registry.calls == []


# ---- profile rules that allow on a node name exactly one device --------------------------------


def test_an_allow_for_a_node_needs_an_exact_device_id() -> None:
    from nox.security.model import Decision, PermissionRequest, Profile, ProfileRule
    from nox.security.permissions import device_rule_applies
    from nox.security.profiles import ProfileError, validate_profile

    for pattern in ("*", "a*", "?" * 32, "A" * 32):
        rule = ProfileRule(id="r", tool="file*", decision=Decision.ALLOW, device=pattern)
        with pytest.raises(ProfileError):
            validate_profile(Profile(id="companion", rules=[rule]))
    exact = ProfileRule(id="e", tool="file*", decision=Decision.ALLOW, device="a" * 32)
    validate_profile(Profile(id="companion", rules=[exact]))
    narrowing = ProfileRule(id="n", tool="file*", decision=Decision.DENY, device="*")
    validate_profile(Profile(id="companion", rules=[narrowing]))
    request = PermissionRequest(
        agent="a", tool="file", action="write", mode="m", risk=Risk.MEDIUM, device="a" * 32
    )
    assert device_rule_applies(exact, request) and device_rule_applies(narrowing, request)
    globbed = ProfileRule(id="g", tool="file*", decision=Decision.ALLOW, device="a*")
    assert not device_rule_applies(globbed, request)  # even if it slipped past validation
