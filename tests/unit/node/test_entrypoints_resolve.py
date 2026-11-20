"""Every `nox.*` module a unit, a script, an installer or the CI names still exists.

A node is started by files no unit test runs: the systemd units and the Windows launcher start
`installer/start_node.pyw`, which imports `nox.node.device.bundle` and then hands over to
`nox.cli`; the console script in `pyproject.toml`, the installer's own calls and the workflows name
modules too. Moving a module without touching one of those files breaks a real device and nothing
else. This reads the files as text and resolves each dotted `nox.` path against `src/nox` - no
import, so it needs neither the extras nor a desktop and gives the same answer on every platform.
"""

from __future__ import annotations

import ast
import re
import textwrap
import tomllib
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
TEXT_SUFFIXES = {".py", ".pyw", ".sh", ".service", ".cmd", ".iss", ".yml", ".yaml", ".toml", ".md"}
#: Where the starting of a node (and the rest of the product) is written down as text.
SCANNED = (
    "src",
    "installer",
    "os",
    "scripts",
    ".github/workflows",
    "config",
    "pyproject.toml",
)
#: A dotted path that starts a name, not one that continues a longer one (`io.github.x.nox.node`).
DOTTED = re.compile(r"(?<![\w.\-/])nox((?:\.[A-Za-z_][A-Za-z0-9_]*)+)")
TOP_LEVEL = {p.stem for p in (SRC / "nox").iterdir() if p.suffix == ".py" or p.is_dir()}


def _files() -> Iterator[Path]:
    for entry in SCANNED:
        root = REPO / entry
        if root.is_file():
            yield root
            continue
        for path in sorted(root.rglob("*")):
            if (
                path.is_file()
                and path.suffix in TEXT_SUFFIXES
                and "__pycache__" not in path.parts
                and "node_modules" not in path.parts
            ):
                yield path


def _module_file(parts: tuple[str, ...]) -> Path | None:
    base = SRC.joinpath(*parts)
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


@cache
def _names(path: Path) -> frozenset[str]:
    """What a module binds at its top level (also inside `if`, `try`, `with`), from its source."""
    found: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                found.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    found.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found.add(node.target.id)
            elif isinstance(node, ast.Import | ast.ImportFrom):
                found.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.If | ast.With):
                visit(node.body)
                visit(getattr(node, "orelse", []))
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)

    visit(ast.parse(path.read_text(encoding="utf-8")).body)
    return frozenset(found)


def unresolved(dotted: str) -> str | None:
    """Why `nox.<dotted>` names nothing in this checkout, or None when it does."""
    tail = dotted.lstrip(".")
    parts = ("nox", *tail.split(".")) if tail else ("nox",)
    for cut in range(len(parts), 0, -1):
        module = _module_file(parts[:cut])
        if module is None:
            continue
        rest = parts[cut:]
        if not rest:
            return None
        if rest[0] in _names(module):
            return None
        return f"{'.'.join(parts[:cut])} has no `{rest[0]}`"
    return f"no module {'.'.join(parts[:2])}"


def _code_strings(path: Path) -> str:
    """The string literals of a Python file that are not docstrings: `python -c` programs, module
    names handed to `importlib`. Comments and docstrings only talk about code."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    literals = (
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    )
    return "\n".join(literals)


def _mentions() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in _files():
        if path.relative_to(REPO).parts[0] == "src":
            if path.suffix != ".py":
                continue
            text = _code_strings(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        for match in DOTTED.finditer(text):
            first = match.group(1).lstrip(".").split(".")[0]
            if first in TOP_LEVEL:
                found.append((path.relative_to(REPO).as_posix(), f"nox{match.group(1)}"))
    return found


def test_the_scan_finds_the_files_that_start_a_node() -> None:
    names = {path.relative_to(REPO).as_posix() for path in _files()}

    assert {
        "installer/start_node.pyw",
        "installer/linux/nox-node.service",
        "installer/linux/nox-node-user.service",
        "installer/nox.iss",
        "pyproject.toml",
        ".github/workflows/ci.yml",
    } <= names
    assert len(_mentions()) > 20


def test_every_nox_path_named_in_a_unit_a_script_or_the_ci_exists() -> None:
    broken = sorted(
        {
            f"{where}: {token} ({why})"
            for where, token in _mentions()
            if (why := unresolved(token.removeprefix("nox"))) is not None
        }
    )

    assert broken == []


def test_the_console_script_points_at_a_function_that_exists() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    targets = project["scripts"]

    assert targets, "pyproject.toml declares no console script"
    for name, target in targets.items():
        module, _, function = target.partition(":")
        assert unresolved(module.removeprefix("nox")) is None, f"{name}: {module}"
        found = _module_file(tuple(module.split(".")))
        assert found is not None
        assert function in _names(found), f"{name}: {module} has no {function}"


def _walk_imports(tree: ast.AST) -> Iterator[tuple[str, tuple[str, ...]]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "nox" or node.module.startswith("nox."):
                yield node.module, tuple(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "nox" or alias.name.startswith("nox."):
                    yield alias.name, ()


def _imports_of(path: Path) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Every absolute `nox` import in a file - lazy ones inside functions included, and those in
    string literals that are Python programs themselves (the scripts a test starts as a child
    process, `python -c` code), which no import of the test module ever reaches."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    yield from _walk_imports(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "import" not in node.value or "nox" not in node.value:
                continue
            if "\n" not in node.value.strip() and ";" not in node.value:
                continue  # one bare statement is data (a fixture to scan), not a program
            try:
                program = ast.parse(textwrap.dedent(node.value))
            except SyntaxError:
                continue
            yield from _walk_imports(program)


def _python_files() -> Iterator[Path]:
    for root in ("src", "scripts", "installer", "evals", "tests"):
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix in {".py", ".pyw"} and "__pycache__" not in path.parts:
                yield path


def test_no_import_of_a_nox_module_dangles() -> None:
    """Imports inside functions (`nox.cli` loads the node lazily) are never run by a unit test
    that does not reach that branch; read them all."""
    broken = []
    for path in _python_files():
        for module, names in _imports_of(path):
            why = unresolved(module.removeprefix("nox"))
            if why is not None:
                broken.append(f"{path.relative_to(REPO).as_posix()}: import {module} ({why})")
                continue
            target = _module_file(tuple(module.split(".")))
            if target is None:
                continue
            for name in names:
                if name == "*" or name in _names(target):
                    continue
                if _module_file((*module.split("."), name)) is None:
                    broken.append(
                        f"{path.relative_to(REPO).as_posix()}: from {module} import {name}"
                    )

    assert broken == []


@pytest.mark.parametrize(
    "token",
    [
        "nox.node.device.bundle.verify_folder",
        "nox.node.wire.protocol.NODE_PROTOCOL",
        "nox.cli.main",
    ],
)
def test_the_resolver_accepts_what_the_launchers_import(token: str) -> None:
    assert unresolved(token.removeprefix("nox")) is None


@pytest.mark.parametrize(
    "token",
    ["nox.node.bundle.verify_folder", "nox.node.protocol", "nox.node.device.bundle.no_such_name"],
)
def test_the_resolver_names_a_moved_or_missing_module(token: str) -> None:
    assert unresolved(token.removeprefix("nox")) is not None


def test_every_extension_the_core_installs_has_its_install_module() -> None:
    """The core imports each extension by a name built at run time (`nox.<name>.install`); a
    moved module shows up there only as a log line and a missing feature."""
    from nox.core.boot.extensions import DEFAULT_EXTENSIONS, extension_module

    for name in DEFAULT_EXTENSIONS:
        module = extension_module(name)
        found = _module_file(tuple(module.split(".")))

        assert found is not None, f"{name}: {module}"
        assert "install" in _names(found), f"{name}: {module} has no install()"


#: `src/nox/...` as the workflows and scripts spell a source path, or as Python joins it.
SLASH_PATH = re.compile(
    r"(?<![\w.\-])src/nox((?:/[A-Za-z0-9_]+)+(?:\.[A-Za-z0-9_.]*[A-Za-z0-9_])?)"
)
JOINED_PATH = re.compile(r'"src"\s*/\s*"nox"((?:\s*/\s*"[A-Za-z0-9_.]+")+)')


def _source_paths() -> list[tuple[str, str]]:
    found = []
    for path in _files():
        if path.relative_to(REPO).parts[0] == "src":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        where = path.relative_to(REPO).as_posix()
        found += [(where, "src/nox" + m.group(1)) for m in SLASH_PATH.finditer(text)]
        for m in JOINED_PATH.finditer(text):
            parts = re.findall(r'"([A-Za-z0-9_.]+)"', m.group(1))
            found.append((where, "src/nox/" + "/".join(parts)))
    return found


def test_every_source_path_a_script_or_the_ci_names_exists() -> None:
    """`sed` in the release workflow and the bundle builder read `protocol.py` by its path."""
    paths = _source_paths()
    missing = sorted({f"{where}: {rel}" for where, rel in paths if not (REPO / rel).exists()})

    assert any(rel.endswith("wire/protocol.py") for _, rel in paths), "the scan found no path"
    assert missing == []
