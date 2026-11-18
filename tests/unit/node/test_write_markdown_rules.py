"""Markdown written on a node may not carry what a note app would run: code blocks in
runnable or unknown languages, note-app and file links, in every spelling, at every
position, also when assembled from appends or padded past a size cap."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.node.tools import NodeRefusal
from tests.unit.node.support_hands import write_file, write_refusal

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
    empty_fs_world: dict[str, Path], text: str
) -> None:
    assert await write_refusal(empty_fs_world, "notes/plan.md", text) == "content_refused"


async def test_ordinary_markdown_is_written(empty_fs_world: dict[str, Path]) -> None:
    text = "# Einkauf\n- Tomaten\n- Kaffee\n\n```text\nsumme = 3\n```\n"
    assert (await write_file(empty_fs_world, "notes/einkauf.md", text))["ok"] is True


# ---- the Markdown scan is line based, and an append is judged on the whole file ------------------


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
    empty_fs_world: dict[str, Path], text: str
) -> None:
    assert await write_refusal(empty_fs_world, "notes/plan.md", text) == "content_refused"


@pytest.mark.parametrize(
    "text",
    [
        "```text\nhallo\n```\n",
        '~~~json\n{"a": 1}\n~~~\n',
        "```\nkein Name\n```\n",
        "Ein `inline code` und ein Preis von 5 $.",
    ],
)
async def test_ordinary_fences_and_code_are_written(
    empty_fs_world: dict[str, Path], text: str
) -> None:
    assert (await write_file(empty_fs_world, "notes/code.md", text))["ok"] is True


async def test_a_block_assembled_from_appends_is_refused(empty_fs_world: dict[str, Path]) -> None:
    assert (await write_file(empty_fs_world, "notes/plan.md", "Start\n"))["ok"] is True
    pieces = ["```", "dataviewjs\n", "dv.paragraph(require('fs'))\n```\n"]
    refusals = []
    for piece in pieces:
        try:
            await write_file(empty_fs_world, "notes/plan.md", piece, append=True)
        except NodeRefusal as exc:
            refusals.append(exc.reason)
    assert refusals and set(refusals) == {"content_refused"}
    assert "dataviewjs" not in (empty_fs_world["root"] / "notes" / "plan.md").read_text(
        encoding="utf-8"
    )


async def test_appending_to_a_huge_markdown_file_is_refused(
    empty_fs_world: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nox.node import hands as hands_module

    monkeypatch.setattr(hands_module, "MAX_WRITE_BYTES", 10)
    (empty_fs_world["root"] / "notes" / "lang.md").write_text("x" * 20, encoding="utf-8")
    assert await _refused_existing(empty_fs_world, "notes/lang.md") == "too_large"


async def _refused_existing(world: dict[str, Path], path: str) -> str:
    with pytest.raises(NodeRefusal) as caught:
        await write_file(world, path, "y", append=True)
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
    empty_fs_world: dict[str, Path], text: str
) -> None:
    assert await write_refusal(empty_fs_world, "notes/plan.md", text) == "content_refused"


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
    empty_fs_world: dict[str, Path], text: str
) -> None:
    assert (await write_file(empty_fs_world, "notes/code.md", text))["ok"] is True


@pytest.mark.parametrize(
    "text",
    ["[Tresor](obsidian://open?vault=x)", "[datei](file:///C:/Windows/x)", "<a href=FILE:x>a</a>"],
)
async def test_note_app_and_file_links_are_refused(
    empty_fs_world: dict[str, Path], text: str
) -> None:
    assert await write_refusal(empty_fs_world, "notes/links.md", text) == "content_refused"


async def test_ordinary_words_with_file_are_written(empty_fs_world: dict[str, Path]) -> None:
    text = "Profile: Alex\nDas Wort Profil und profile: stehen hier."
    assert (await write_file(empty_fs_world, "notes/profil.md", text))["ok"] is True


async def test_an_append_refusal_says_what_in_the_file_blocks_it(
    empty_fs_world: dict[str, Path],
) -> None:
    (empty_fs_world["root"] / "notes" / "alt.md").write_text(
        "# Alt\n```dataview\nLIST\n```\n", encoding="utf-8"
    )
    with pytest.raises(NodeRefusal) as caught:
        await write_file(empty_fs_world, "notes/alt.md", "neu\n", append=True)
    assert caught.value.reason == "content_refused"
    assert "'dataview' code block" in caught.value.detail
    assert "already in the file" in caught.value.detail


# ---- a cap is never a skip - padding past it is refused ------------------------------------------


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
async def test_padding_past_an_old_cap_is_refused(
    empty_fs_world: dict[str, Path], text: str
) -> None:
    assert await write_refusal(empty_fs_world, "notes/plan.md", text) == "content_refused"


async def test_a_fence_with_trailing_blanks_and_no_language_is_written(
    empty_fs_world: dict[str, Path],
) -> None:
    text = "```   \nhallo\n```\t\n"
    assert (await write_file(empty_fs_world, "notes/leer.md", text))["ok"] is True
