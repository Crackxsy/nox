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
        "```DataViewJS\nTABLE file.name\n```",
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
    text = "# Einkauf\n- Tomaten\n- Kaffee\n\n```text\nsumme = 3\n```\n"
    assert (await _write(world, "notes/einkauf.md", text))["ok"] is True


# ---- structured text: bounded, and refused before anyone is asked -------------------------------


async def test_deeply_nested_or_huge_structured_text_is_refused(world: dict[str, Path]) -> None:
    assert await _refused(world, "notes/tief.json", "[" * 60_000) == "invalid_content"
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
    from nox.node.install import _write_precheck, _write_target
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
        precheck=_write_precheck,
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


# ---- review: the Markdown scan is line based, and an append is judged on the whole file ---------


@pytest.mark.parametrize(
    "text",
    [
        "~~~dataviewjs\ndv.paragraph(1)\n~~~\n",
        "````dataviewjs\ndv.paragraph(1)\n````\n",
        "   ```dataviewjs\ndv.paragraph(1)\n```\n",
        "\t```dataviewjs\ndv.paragraph(1)\n```\n",
        "``` dataviewjs\ndv.paragraph(1)\n```\n",
        "```{dataviewjs}\nx\n```\n",
        "```js-engine\nreturn 1\n```\n",
        "~~~ DataView-JS\nLIST\n~~~\n",
        "> ```dataviewjs\n> dv.paragraph(1)\n> ```\n",
        "- ```dataviewjs\n  dv.paragraph(1)\n  ```\n",
        "1. ~~~dataviewjs\n   dv.paragraph(1)\n   ~~~\n",
        "        ```dataviewjs\n        x\n",
        "``````\nnot closed\n```dataviewjs\nx\n```\n",
        "```py`x\n```dataviewjs\nx\n```\n",
        "````\n```dataviewjs\n````\n",
        "Ich nutze das Plugin Templater.",
        "Wert: `=this.file.name`",
        "~~ $= 1",
        "Titel %> tp.date.now()",
        "```meta-bind-js\nx\n```\n",
        "Heute ist `` $= dv.date('today') ``.",
        "Heute ist `  $=1`.",
        "&lt;% tp.date.now() %&gt;",
        '<img src="x.png" onerror="x()">',
        "<iframe src=x></iframe>",
    ],
)
async def test_every_markdown_spelling_of_runnable_code_is_refused(
    world: dict[str, Path], text: str
) -> None:
    assert await _refused(world, "notes/plan.md", text) == "content_refused"


@pytest.mark.parametrize(
    "text",
    [
        "```text\nhallo\n```\n",
        '~~~json\n{"a": 1}\n~~~\n',
        "```\nkein Name\n```\n",
        "Ein `inline code` und ein Preis von 5 $.",
    ],
)
async def test_ordinary_fences_and_code_are_written(world: dict[str, Path], text: str) -> None:
    assert (await _write(world, "notes/code.md", text))["ok"] is True


async def test_a_block_assembled_from_appends_is_refused(world: dict[str, Path]) -> None:
    assert (await _write(world, "notes/plan.md", "Start\n"))["ok"] is True
    pieces = ["```", "dataviewjs\n", "dv.paragraph(require('fs'))\n```\n"]
    refusals = []
    for piece in pieces:
        try:
            await _write(world, "notes/plan.md", piece, append=True)
        except NodeRefusal as exc:
            refusals.append(exc.reason)
    assert refusals and set(refusals) == {"content_refused"}
    assert "dataviewjs" not in (world["root"] / "notes" / "plan.md").read_text(encoding="utf-8")


async def test_appending_to_a_huge_markdown_file_is_refused(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import hands as hands_module

    monkeypatch.setattr(hands_module, "MAX_WRITE_BYTES", 10)
    (world["root"] / "notes" / "lang.md").write_text("x" * 20, encoding="utf-8")
    assert await _refused_existing(world, "notes/lang.md") == "too_large"


async def _refused_existing(world: dict[str, Path], path: str) -> str:
    with pytest.raises(NodeRefusal) as caught:
        await _write(world, path, "y", append=True)
    return caught.value.reason


def test_the_markdown_check_is_position_independent() -> None:
    from nox.node.safe_fs import markdown_runs_code

    # Wherever the token stands - quoted, listed, indented, after a broken fence - it counts.
    for prefix in ("", "> ", "- ", "1. ", "        ", "> - ", "\t"):
        assert markdown_runs_code(f"Text\n{prefix}```dataviewjs\n{prefix}x\n{prefix}```\n")
    assert not markdown_runs_code("```text\nhallo\n```\n~~~yaml\na: 1\n~~~\n")


@pytest.mark.parametrize(
    "text",
    [
        "```jsx\nreturn <div/>\n```\n",
        "```tp-run\nx\n```\n",
        "```datacore\nreturn 1\n```\n",
        "```dataview\nLIST FROM #einkauf\n```\n",
        "```meta-bind-js\nx\n```\n",
        "```{.dataviewjs}\nx\n```\n",
        "```  DataviewJS\nx\n```\n",
        "> ```eigenesplugin\n> x\n> ```\n",
        "- ~~~eigenesplugin\n  x\n  ~~~\n",
        "Text ````  {.irgendwas} mehr",
        "```python:titel\nx\n```\n",
        '```mermaid\ngraph TD; A-->B\nclick A call alert("x")\n```\n',
        "```html\n<b>x</b>\n```\n",
        "```math\nx^2\n```\n",
        "```latex\nx\n```\n",
        "```xml\n<a/>\n```\n",
        "```css\nbody{}\n```\n",
    ],
)
async def test_a_code_block_in_an_unknown_language_is_refused(
    world: dict[str, Path], text: str
) -> None:
    assert await _refused(world, "notes/plan.md", text) == "content_refused"


@pytest.mark.parametrize(
    "text",
    [
        "```json\n{}\n```\n",
        "~~~yaml\na: 1\n~~~\n",
        "```\nohne Sprache\n```\n",
        "> ```csv\n> a,b\n> ```\n",
        "```{.text}\nx\n```\n",
    ],
)
async def test_code_blocks_in_common_languages_are_written(
    world: dict[str, Path], text: str
) -> None:
    assert (await _write(world, "notes/code.md", text))["ok"] is True


# ---- review: linear, bounded, off the event loop ------------------------------------------------


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

    from nox.node.safe_fs import markdown_runs_code

    started = time.perf_counter()
    markdown_runs_code(ADVERSARIAL[name])
    assert time.perf_counter() - started < 0.5, name


@pytest.mark.parametrize("path", ["notes/lang.md", "notes/lang.txt", "notes/lang.csv"])
async def test_text_over_the_cap_is_refused(world: dict[str, Path], path: str) -> None:
    from nox.node.safe_fs import MAX_TEXT_BYTES

    assert await _refused(world, path, "a" * (MAX_TEXT_BYTES + 1)) == "too_large"
    assert (await _write(world, path, "a" * 1000))["ok"] is True


async def test_the_core_runs_the_text_check_in_a_thread_with_a_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time

    from nox.node import tools as tools_module
    from nox.node.install import _write_target
    from nox.node.router import routed_spec

    seen: list[str] = []

    def slow(_payload: dict[str, Any]) -> str | None:
        seen.append(threading.current_thread().name)
        time.sleep(0.3)
        return None

    monkeypatch.setattr(tools_module, "CHECK_TIMEOUT_S", 0.05)
    laptop = _entry("a" * 32, "Laptop", tools={"file.write": "medium"})
    spec = routed_spec(
        name="file.write",
        description="t",
        risk=Risk.MEDIUM,
        side_effects=True,
        registry=FakeRegistry([laptop]),  # type: ignore[arg-type]
        input_model=WriteArgs,
        node_target=_write_target,
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

    from nox.node.receipts import ReceiptSigner, ReceiptVerifier

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


# ---- review: renamed vault config folders, the home folder, more link schemes -------------------


@pytest.mark.parametrize(
    "signature", ["app.json", "community-plugins.json", "plugins", "config.edn"]
)
async def test_a_vault_with_a_renamed_config_folder_is_found_by_its_signature(
    world: dict[str, Path], signature: str
) -> None:
    vault = world["root"] / "Tresor"
    config = vault / ".meine-einstellungen"
    config.mkdir(parents=True)
    if signature == "plugins":
        (config / signature).mkdir()
    else:
        (config / signature).write_text("{}", encoding="utf-8")
    assert await _refused(world, "Tresor/notiz.md") == "app_workspace_refused"


async def test_a_renamed_vault_around_the_allowed_folder_is_found(tmp_path: Path) -> None:
    vault = tmp_path / "Tresor"
    (vault / ".config-x").mkdir(parents=True)
    (vault / ".config-x" / "workspace.json").write_text("{}", encoding="utf-8")
    root = vault / "Nox"
    root.mkdir()
    world = {"root": root, "node": tmp_path / "node-home"}
    assert await _refused(world, "liste.txt") == "app_workspace_refused"


async def test_app_settings_in_the_home_folder_do_not_count_as_a_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".vscode").mkdir(parents=True)  # VS Code's own settings, not a workspace
    (home / ".idea").mkdir()
    root = home / "Dokumente" / "Nox"
    root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    world = {"root": root, "node": tmp_path / "node-home"}
    assert (await _write(world, "liste.txt"))["ok"] is True
    (home / ".git").mkdir()  # a home folder that is a repository still counts
    assert await _refused(world, "zweite.txt") == "working_tree_refused"


@pytest.mark.parametrize(
    "text",
    ["[Tresor](obsidian://open?vault=x)", "[datei](file:///C:/Windows/x)", "<a href=FILE:x>a</a>"],
)
async def test_note_app_and_file_links_are_refused(world: dict[str, Path], text: str) -> None:
    assert await _refused(world, "notes/links.md", text) == "content_refused"


async def test_ordinary_words_with_file_are_written(world: dict[str, Path]) -> None:
    text = "Profile: Alex\nDas Wort Profil und profile: stehen hier."
    assert (await _write(world, "notes/profil.md", text))["ok"] is True


async def test_an_append_refusal_says_what_in_the_file_blocks_it(world: dict[str, Path]) -> None:
    (world["root"] / "notes" / "alt.md").write_text(
        "# Alt\n```dataview\nLIST\n```\n", encoding="utf-8"
    )
    with pytest.raises(NodeRefusal) as caught:
        await _write(world, "notes/alt.md", "neu\n", append=True)
    assert caught.value.reason == "content_refused"
    assert "'dataview' code block" in caught.value.detail
    assert "already in the file" in caught.value.detail


# ---- review: a cap is never a skip - padding past it is refused --------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "```" + " " * 40 + "datacore\nx\n```\n",
        "```" + " " * 100_000 + "datacore\nx\n```\n",
        "```" + "\t" * 30 + "{" + " " * 30 + ".datacore}\nx\n```\n",
        "```text" + "x" * 100 + "\nx\n```\n",
        "```" + "t" * 65 + "\nx\n```\n",
        "```\x0bdatacore\nx\n```\n",
        "<img " + "a" * 300 + " onerror=x>",
        "<img " + "a" * 100_000 + " onload =x>",
        "`" + " " * 100 + "$= dv.x",
        "`" + "\n" * 70 + "=this.file",
    ],
    ids=lambda text: f"{len(text)}-{text.strip()[:6]!r}",
)
async def test_padding_past_an_old_cap_is_refused(world: dict[str, Path], text: str) -> None:
    assert await _refused(world, "notes/plan.md", text) == "content_refused"


async def test_a_fence_with_trailing_blanks_and_no_language_is_written(
    world: dict[str, Path],
) -> None:
    text = "```   \nhallo\n```\t\n"
    assert (await _write(world, "notes/leer.md", text))["ok"] is True


# ---- final review: an abandoned check changes nothing; no skip in the vault scan ----------------


async def test_a_slow_text_check_never_holds_up_the_stop(
    listener: Any,
    pairing: Any,
    tls: Any,
    tmp_path: Path,
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time

    from nox.node import hands as hands_module
    from tests.unit.node.test_c4 import Confirmer, call, session_of
    from tests.unit.node.test_c5 import FakeSandbox, start

    def slow(path: str, text: str, *, append: bool) -> str | None:
        time.sleep(1.0)  # a worker thread: the event loop goes on
        return None

    monkeypatch.setattr(hands_module, "write_text_refusal", slow)
    agent, task, stop, _audit = await start(
        listener, pairing, tls, tmp_path, world, FakeSandbox(), Confirmer("allow")
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
    world: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time

    from nox.node import hands as hands_module
    from nox.node import tools as tools_module
    from tests.unit.node.test_c4 import Confirmer, call, session_of
    from tests.unit.node.test_c5 import FakeSandbox, start

    def slow(path: str, text: str, *, append: bool) -> str | None:
        time.sleep(0.4)
        return None

    monkeypatch.setattr(hands_module, "write_text_refusal", slow)
    monkeypatch.setattr(tools_module, "CHECK_TIMEOUT_S", 0.1)
    agent, task, stop, audit = await start(
        listener, pairing, tls, tmp_path, world, FakeSandbox(), Confirmer("allow")
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
        assert not (world["root"] / "a.txt").exists()
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


def test_one_receipt_is_used_once_even_from_many_threads(tmp_path: Path) -> None:
    import threading

    from nox.node.receipts import ReceiptSigner, ReceiptVerifier

    signer = ReceiptSigner(tmp_path / "r")
    verifier = ReceiptVerifier(signer.public_key, device_id="d" * 32, core_id="c" * 64)
    tools = hand_tools(NodeHands(folders=lambda: [], protected=(tmp_path,), sandbox=lambda: None))
    gate = NodeGate(
        tools, NodeManifest(groups={g: g == "files" for g in GROUPS}), None, receipts=verifier
    )
    args = {"path": "a.txt", "text": "x", "append": False, "overwrite": False}
    meta = NoxCallMeta(
        **signed(signer, "file.write", args, risk="medium", device_id="d" * 32, core_id="c" * 64)
    )
    results: list[bool] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(gate.check("file.write", args, meta=meta).allowed)
        )
        for _ in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1  # replay protection holds under concurrency


async def test_a_renamed_vault_behind_a_thousand_notes_is_still_found(
    world: dict[str, Path],
) -> None:
    vault = world["root"] / "Tresor"
    vault.mkdir()
    for i in range(1000):  # many entries, some sorting before and after the dot-folder
        (vault / f"{i:04}-notiz.md").write_text("x", encoding="utf-8")
        if i == 500:
            (vault / ".zz-einstellungen").mkdir()
            (vault / ".zz-einstellungen" / "app.json").write_text("{}", encoding="utf-8")
    (vault / "~ende.md").write_text("x", encoding="utf-8")
    assert await _refused(world, "Tresor/neu.md") == "app_workspace_refused"


@pytest.mark.parametrize(
    "config",
    [(".obsidian", ["app.json"]), (".tresor-config", ["app.json", "workspace.json"])],
)
async def test_a_vault_at_the_home_folder_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: tuple[str, list[str]]
) -> None:
    home = tmp_path / "home"
    folder, files = config
    (home / folder).mkdir(parents=True)
    for name in files:
        (home / folder / name).write_text("{}", encoding="utf-8")
    root = home / "Notizen"
    root.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    world = {"root": root, "node": tmp_path / "node-home"}
    assert await _refused(world, "liste.txt") == "app_workspace_refused"


async def test_an_app_folder_with_plugins_at_home_is_no_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".irgendeine-app" / "plugins").mkdir(parents=True)  # an app's own plugins
    root = home / "Notizen"
    root.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    world = {"root": root, "node": tmp_path / "node-home"}
    assert (await _write(world, "liste.txt"))["ok"] is True


async def test_structured_text_is_capped_lower_than_plain_text(world: dict[str, Path]) -> None:
    from nox.node.safe_fs import MAX_STRUCTURED_BYTES

    big = '{"a": "' + "x" * MAX_STRUCTURED_BYTES + '"}'
    assert await _refused(world, "notes/gross.json", big) == "too_large"
    assert (await _write(world, "notes/gross.md", "x" * (MAX_STRUCTURED_BYTES + 10)))["ok"]
