"""The node code is three packages, and every file belongs to exactly one of them.

`nox.node.wire` is what both sides agree on, `nox.node.device` runs on a device and
`nox.node.hub` runs on the core (docs/ARCHITECTURE-NODES.md, "Module map"). A loose module next
to them, or a file in two of them, would make the import contract in `pyproject.toml` say less
than it seems to. The contract itself runs in CI as `lint-imports`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import nox.node

NODE_DIR = Path(nox.node.__file__).resolve().parent
LAYERS = ("wire", "device", "hub")
#: The sub-packages of a layer. The core's extension loader names `nox.node.hub.install`
#: (`MODULE_OF`), and the wiring is cut into modules inside it. On the device, a module that grew
#: past the size rule (docs/CODE_STANDARDS.md) became a package of modules, one per responsibility.
SUBPACKAGES = {"hub": ("install",), "device": ("hands", "safe_fs", "update")}


def _sources() -> list[Path]:
    return sorted(
        path
        for path in NODE_DIR.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )


def test_the_node_package_holds_the_three_layers_and_nothing_else() -> None:
    entries = {path.name for path in NODE_DIR.iterdir() if path.name != "__pycache__"}

    assert entries == {"__init__.py", *LAYERS}


def test_every_file_lies_in_exactly_one_layer() -> None:
    outside = [
        path.relative_to(NODE_DIR).as_posix()
        for path in _sources()
        if path.relative_to(NODE_DIR).parts[0] not in LAYERS
        and path.relative_to(NODE_DIR).as_posix() != "__init__.py"
    ]

    assert outside == []


def test_every_layer_is_a_package_with_python_files_only() -> None:
    for layer in LAYERS:
        files = [path for path in _sources() if path.relative_to(NODE_DIR).parts[0] == layer]

        assert (NODE_DIR / layer / "__init__.py") in files
        assert {path.suffix for path in files} == {".py"}
        folders = {path.parent for path in files} - {NODE_DIR / layer}
        assert folders == {NODE_DIR / layer / name for name in SUBPACKAGES.get(layer, ())}


def test_no_module_name_exists_in_two_layers() -> None:
    seen: dict[str, str] = {}
    twice = []
    for layer in LAYERS:
        for path in (NODE_DIR / layer).glob("*.py"):
            if path.stem == "__init__":
                continue
            if path.stem in seen:
                twice.append(f"{path.stem}: {seen[path.stem]} and {layer}")
            seen[path.stem] = layer

    assert twice == []


def test_importing_a_layer_imports_nothing_else() -> None:
    # `installer/start_node.pyw` imports `nox.node.device.bundle` from the installed base before
    # any release is trusted: the package it passes through must stay empty.
    for layer in LAYERS:
        tree = ast.parse((NODE_DIR / layer / "__init__.py").read_text(encoding="utf-8"))

        assert [n for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)] == []
