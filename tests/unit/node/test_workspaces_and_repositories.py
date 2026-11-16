"""App workspaces and repositories take no writes: a note vault, an editor workspace or a
version-controlled folder is recognised by its markers - also when it lies above the
allowed folder, at the home folder or under a renamed configuration folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.node.home import NodeHome
from tests.unit.node.support_hands import write_file, write_refusal

# ---- app workspaces ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker", [".obsidian", ".logseq", "logseq/config.edn", ".foam", ".dendron", ".vscode", ".idea"]
)
async def test_nothing_in_an_app_workspace_is_written(
    empty_fs_world: dict[str, Path], marker: str
) -> None:
    vault = empty_fs_world["root"] / "Tresor"
    (vault / "Tagebuch").mkdir(parents=True)
    target = vault / marker
    if marker.endswith(".edn"):
        target.parent.mkdir()
        target.write_text("{}", encoding="utf-8")
    else:
        target.mkdir()
    for path in ("Tresor/notiz.md", "Tresor/Tagebuch/heute.md"):
        assert await write_refusal(empty_fs_world, path) == "app_workspace_refused"
    assert (await write_file(empty_fs_world, "notes/neben.md"))[
        "ok"
    ] is True  # a plain folder beside it


@pytest.mark.parametrize("marker", [".jj", ".bzr", "_darcs", ".sl", ".git", ".hg", ".svn"])
async def test_every_version_control_marker_counts(
    empty_fs_world: dict[str, Path], marker: str
) -> None:
    (empty_fs_world["root"] / "projekt" / marker).mkdir(parents=True)
    assert await write_refusal(empty_fs_world, "projekt/liste.txt") == "working_tree_refused"


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
        assert await write_refusal(world, "liste.txt") == reason


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


# ---- review: renamed vault config folders, the home folder, more link schemes -------------------


@pytest.mark.parametrize(
    "signature", ["app.json", "community-plugins.json", "plugins", "config.edn"]
)
async def test_a_vault_with_a_renamed_config_folder_is_found_by_its_signature(
    empty_fs_world: dict[str, Path], signature: str
) -> None:
    vault = empty_fs_world["root"] / "Tresor"
    config = vault / ".meine-einstellungen"
    config.mkdir(parents=True)
    if signature == "plugins":
        (config / signature).mkdir()
    else:
        (config / signature).write_text("{}", encoding="utf-8")
    assert await write_refusal(empty_fs_world, "Tresor/notiz.md") == "app_workspace_refused"


async def test_a_renamed_vault_around_the_allowed_folder_is_found(tmp_path: Path) -> None:
    vault = tmp_path / "Tresor"
    (vault / ".config-x").mkdir(parents=True)
    (vault / ".config-x" / "workspace.json").write_text("{}", encoding="utf-8")
    root = vault / "Nox"
    root.mkdir()
    world = {"root": root, "node": tmp_path / "node-home"}
    assert await write_refusal(world, "liste.txt") == "app_workspace_refused"


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
    assert (await write_file(world, "liste.txt"))["ok"] is True
    (home / ".git").mkdir()  # a home folder that is a repository still counts
    assert await write_refusal(world, "zweite.txt") == "working_tree_refused"


async def test_a_renamed_vault_behind_a_thousand_notes_is_still_found(
    empty_fs_world: dict[str, Path],
) -> None:
    vault = empty_fs_world["root"] / "Tresor"
    vault.mkdir()
    for i in range(1000):  # many entries, some sorting before and after the dot-folder
        (vault / f"{i:04}-notiz.md").write_text("x", encoding="utf-8")
        if i == 500:
            (vault / ".zz-einstellungen").mkdir()
            (vault / ".zz-einstellungen" / "app.json").write_text("{}", encoding="utf-8")
    (vault / "~ende.md").write_text("x", encoding="utf-8")
    assert await write_refusal(empty_fs_world, "Tresor/neu.md") == "app_workspace_refused"


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
    assert await write_refusal(world, "liste.txt") == "app_workspace_refused"


async def test_an_app_folder_with_plugins_at_home_is_no_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".irgendeine-app" / "plugins").mkdir(parents=True)  # an app's own plugins
    root = home / "Notizen"
    root.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    world = {"root": root, "node": tmp_path / "node-home"}
    assert (await write_file(world, "liste.txt"))["ok"] is True
