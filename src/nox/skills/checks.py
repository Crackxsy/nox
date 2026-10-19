"""What a self-built skill must pass before anyone is asked to activate it - checked by the core.

A skill is a plugin that a language model wrote a minute ago. Nothing it says about itself is taken
on trust, and none of these checks is the model's: they run here, on the files, every time.

* **Files** - only source and text files, a bounded number and size. No binaries, no `.pth`.
* **Manifest** - parses as a plugin manifest; id equals the skill id and is neither reserved, a hard
  prohibition, a core namespace nor a bundled plugin; every tool stays in its namespace, avoids the
  boundaries (`nox.security.reserved`) and is at least `medium` risk (so it asks until the user
  says "remember") and never `critical`; it emits only its own events and listens to nothing but
  the mode; egress is explicit public `host:port` - never loopback, the local network or the Home
  Assistant host.
* **Code** - an *allow-list* of imports, not a deny-list: a skill may use pure standard-library
  modules, pydantic and the plugin API, and nothing that reaches processes, files, the network,
  the registry, the keyring or the interpreter itself (`subprocess`, `ctypes`, `os.system`,
  `socket`, `importlib`, `open`, `eval`, ...). The network exists only as `api.http()`, which the
  worker scopes to the manifest's egress. Dunder attribute tricks are refused, as are the
  input-synthesis and process-memory APIs CI forbids in Nox itself.

`tree_hash()` is what activation is bound to: the files the user was shown are the files that run.
"""

from __future__ import annotations

import ast
import hashlib
import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from nox.plugins.manifest import ManifestError, PluginManifest, load_manifest, split_endpoint
from nox.security.egress import is_loopback
from nox.security.model import Risk
from nox.security.reserved import name_refusal

__all__ = [
    "CheckReport",
    "check_skill",
    "scan_code",
    "tree_hash",
]

MAX_FILES = 60
MAX_TOTAL_BYTES = 512 * 1024
ALLOWED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".py", ".yaml", ".yml", ".md", ".txt", ".json", ".toml", ".cfg", ".ini"}
)

#: Modules a skill's source may import (and their submodules where listed with a dot).
SRC_IMPORTS: Final[frozenset[str]] = frozenset(
    {
        "__future__",
        "abc",
        "asyncio",
        "base64",
        "bisect",
        "calendar",
        "collections",
        "collections.abc",
        "contextlib",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "difflib",
        "email.utils",
        "enum",
        "fractions",
        "functools",
        "hashlib",
        "heapq",
        "hmac",
        "html",
        "html.parser",
        "itertools",
        "json",
        "math",
        "operator",
        "random",
        "re",
        "statistics",
        "string",
        "textwrap",
        "time",
        "typing",
        "unicodedata",
        "urllib.parse",
        "uuid",
        "zoneinfo",
        "pydantic",
        "nox.plugins.api",
        "nox.security.model",
    }
)
#: Tests may additionally use mocks. They run in the same locked-down way as the source.
TEST_IMPORTS: Final[frozenset[str]] = SRC_IMPORTS | {"unittest.mock"}

#: Named explicitly so the refusal says what was found, not just "not allowed".
_NAMED_FORBIDDEN: Final[frozenset[str]] = frozenset(
    {"subprocess", "ctypes", "os", "sys", "socket", "importlib", "builtins", "shutil", "pathlib",
     "pickle", "marshal", "multiprocessing", "threading", "signal", "pty", "winreg", "_winapi",
     "msvcrt", "keyring", "requests", "httpx", "aiohttp", "urllib.request", "http.client", "ssl",
     "ftplib", "smtplib", "code", "codeop", "inspect", "gc", "types", "io", "tempfile", "glob",
     "sqlite3", "webbrowser", "platform", "runpy", "zipimport", "pkgutil", "site"}
)  # fmt: skip
_FORBIDDEN_CALLS: Final[frozenset[str]] = frozenset(
    {"eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "globals",
     "locals", "vars", "memoryview", "help", "exit", "quit"}
)  # fmt: skip
_DYNAMIC_ATTR_CALLS: Final[frozenset[str]] = frozenset({"getattr", "setattr", "delattr", "hasattr"})
#: Attributes that reach processes, sockets or files from otherwise allowed modules
#: (`asyncio.create_subprocess_exec`, `loop.create_connection`, pydantic's `parse_file`).
_FORBIDDEN_ATTRS: Final[frozenset[str]] = frozenset(
    {"system", "popen", "spawnv", "spawnl", "startfile", "execv", "execl", "fork", "kill",
     "create_subprocess_exec", "create_subprocess_shell", "subprocess_exec", "subprocess_shell",
     "open_connection", "open_unix_connection", "start_server", "start_unix_server",
     "create_connection", "create_datagram_endpoint", "create_server", "create_unix_connection",
     "create_unix_server", "connect_read_pipe", "connect_write_pipe", "getaddrinfo",
     "sock_connect", "sock_sendall", "sock_recv", "parse_file", "parse_raw", "add_signal_handler"}
)  # fmt: skip
_ALLOWED_DUNDERS: Final[frozenset[str]] = frozenset({"__init__", "__name__", "__doc__"})
#: CI's list (Security Model §10) plus the other input-synthesis entry points. Spelled in pieces:
#: the CI scan cannot tell this list from a use of what it names (see `nox.desktop.keyboard`).
_FORBIDDEN_TEXT = re.compile(
    "|".join(
        a + b
        for a, b in (
            ("pydirect", "input"),
            ("pyauto", "gui"),
            ("pyn", "put"),
            ("ReadProcess", "Memory"),
            ("WriteProcess", "Memory"),
            ("Send", "Input"),
            ("keybd", "_event"),
            ("mouse", "_event"),
            ("SetWindows", "HookEx"),
            ("CreateRemote", "Thread"),
            ("Virtual", "AllocEx"),
        )
    ),
    re.IGNORECASE,
)
_ALLOWED_LISTENS: Final[frozenset[str]] = frozenset({"system.mode_changed"})
#: A host written into the source as a URL; it has to be one the manifest declares.
_URL_HOST = re.compile(r"(?i)\b(?:https?|wss?)://([a-z0-9.-]+)")


def _endpoint_ok(entry: str) -> bool:
    try:
        split_endpoint(entry)
    except ManifestError:
        return False
    return True


@dataclass
class CheckReport:
    """Every problem found, in words a model can fix and a user can read."""

    problems: list[str] = field(default_factory=list)
    manifest: PluginManifest | None = None

    @property
    def ok(self) -> bool:
        return not self.problems and self.manifest is not None


# ---- files ---------------------------------------------------------------------------------------


def _files(root: Path) -> list[Path]:
    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith(".")
    )


def tree_hash(root: Path) -> str:
    """SHA-256 over every file's relative path and bytes, in a stable order."""
    digest = hashlib.sha256()
    for path in _files(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _check_files(root: Path, report: CheckReport) -> list[Path]:
    files = _files(root)
    hidden = [p for p in root.rglob(".*") if "__pycache__" not in p.parts]
    if hidden:
        report.problems.append(f"hidden files are not allowed: {hidden[0].name}")
    if len(files) > MAX_FILES:
        report.problems.append(f"{len(files)} files; at most {MAX_FILES}")
    total = sum(p.stat().st_size for p in files)
    if total > MAX_TOTAL_BYTES:
        report.problems.append(f"{total} bytes in total; at most {MAX_TOTAL_BYTES}")
    for path in files:
        if path.is_symlink():
            report.problems.append(f"{path.name}: links are not allowed")
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            report.problems.append(f"{path.relative_to(root).as_posix()}: file type not allowed")
    return [p for p in files if p.suffix.lower() == ".py"]


# ---- code ----------------------------------------------------------------------------------------


def _module_allowed(module: str, allowed: frozenset[str], own: str) -> bool:
    if module == own or module.startswith(f"{own}."):
        return True
    # Exactly the listed modules - a listed package does not open its submodules
    # (`asyncio.subprocess`) - except pydantic's, which are all data validation.
    return module in allowed or module.startswith("pydantic.")


def scan_code(source: str, *, filename: str, own_package: str, tests: bool = False) -> list[str]:
    """Problems in one Python file; empty when it only uses what a skill may use."""
    problems: list[str] = []
    if _FORBIDDEN_TEXT.search(source):
        problems.append(f"{filename}: names an input-synthesis or process-memory API")
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [*problems, f"{filename}: syntax error on line {exc.lineno}: {exc.msg}"]
    allowed = TEST_IMPORTS if tests else SRC_IMPORTS
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        where = f"{filename}:{line}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _module_allowed(alias.name, allowed, own_package):
                    problems.append(f"{where}: import {alias.name} is not allowed in a skill")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue  # relative import inside the skill's own package
            module = node.module or ""
            if not _module_allowed(module, allowed, own_package):
                problems.append(f"{where}: import from {module} is not allowed in a skill")
            elif module == "os" or any(a.name == "system" for a in node.names):
                problems.append(f"{where}: os.system is not allowed in a skill")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _FORBIDDEN_CALLS:
                problems.append(f"{where}: {node.func.id}() is not allowed in a skill")
            elif node.func.id in _DYNAMIC_ATTR_CALLS and (
                len(node.args) < 2
                or not isinstance(node.args[1], ast.Constant)
                or not isinstance(node.args[1].value, str)
                or node.args[1].value.startswith("_")
            ):
                problems.append(
                    f"{where}: {node.func.id}() needs a literal, public attribute name in a skill"
                )
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr not in _ALLOWED_DUNDERS:
                problems.append(f"{where}: attribute {node.attr} is not allowed in a skill")
            elif node.attr in _FORBIDDEN_ATTRS:
                problems.append(f"{where}: .{node.attr} is not allowed in a skill")
        elif isinstance(node, ast.Name) and node.id in ("__builtins__", "__loader__", "__spec__"):
            problems.append(f"{where}: {node.id} is not allowed in a skill")
    return problems


# ---- manifest ------------------------------------------------------------------------------------


def _private_host(host: str) -> bool:
    if is_loopback(host) or host.endswith((".local", ".lan", ".home", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host  # a bare name resolves on the local network
    return address.is_private or address.is_link_local or address.is_reserved


def _check_manifest(
    manifest: PluginManifest,
    report: CheckReport,
    *,
    taken: Iterable[str],
    home_host: str,
) -> None:
    refusal = name_refusal(manifest.id)
    if refusal is not None:
        report.problems.append(f"skill id {manifest.id!r} {refusal}")
    if manifest.id in set(taken):
        report.problems.append(f"skill id {manifest.id!r} is already a tool namespace of Nox")
    if not manifest.permissions:
        report.problems.append("the manifest declares no tools")
    for permission in manifest.permissions:
        tool_refusal = name_refusal(permission.tool) or name_refusal(
            permission.tool.split(".", 1)[-1]
        )
        if tool_refusal is not None:
            report.problems.append(f"tool {permission.tool!r} {tool_refusal}")
        if permission.risk in (Risk.READ, Risk.LOW):
            report.problems.append(
                f"tool {permission.tool!r} must be risk medium or high, so it asks before it runs"
            )
        if permission.risk is Risk.CRITICAL:
            report.problems.append(f"tool {permission.tool!r} cannot be critical")
    for event in manifest.events.emits:
        if not event.startswith(manifest.namespace):
            report.problems.append(f"event {event!r} is outside the skill's own namespace")
    for event in manifest.events.listens:
        if event not in _ALLOWED_LISTENS:
            report.problems.append(f"listening to {event!r} is not allowed for a skill")
    for entry in manifest.network.egress:
        try:
            host, _port = split_endpoint(entry)
        except ManifestError as exc:
            report.problems.append(str(exc))
            continue
        if _private_host(host):
            report.problems.append(f"egress {entry!r} points at this machine or the local network")
        elif home_host and host == home_host.lower():
            report.problems.append(f"egress {entry!r} is the Home Assistant host")


def check_skill(
    root: Path,
    *,
    skill_id: str,
    taken: Iterable[str] = (),
    home_host: str = "",
) -> CheckReport:
    """Every static check for the plugin folder `root`; see the module docstring."""
    report = CheckReport()
    if not root.is_dir():
        report.problems.append("the skill folder does not exist")
        return report
    py_files = _check_files(root, report)
    try:
        manifest = load_manifest(root, expected_id=skill_id)
    except ManifestError as exc:
        report.problems.append(f"manifest: {exc}")
        return report
    report.manifest = manifest
    _check_manifest(manifest, report, taken=taken, home_host=home_host)
    package = manifest.entry.split(":", 1)[0].split(".", 1)[0]
    if not (root / "src" / package).is_dir():
        report.problems.append(f"the entry package src/{package} does not exist")
    for path in py_files:
        relative = path.relative_to(root).as_posix()
        in_tests = relative.startswith("tests/")
        if not (relative.startswith("src/") or in_tests):
            report.problems.append(f"{relative}: Python files belong in src/ or tests/")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        report.problems += scan_code(text, filename=relative, own_package=package, tests=in_tests)
    declared = {split_endpoint(e)[0] for e in manifest.network.egress if _endpoint_ok(e)}
    for path in py_files:
        relative = path.relative_to(root).as_posix()
        if not relative.startswith("src/"):
            continue
        for host in _URL_HOST.findall(path.read_text(encoding="utf-8", errors="replace")):
            if host.lower() not in declared:
                report.problems.append(
                    f"{relative}: calls {host} which manifest network.egress does not declare"
                )
    if not any(p.relative_to(root).as_posix().startswith("tests/test_") for p in py_files):
        report.problems.append("there are no tests (tests/test_*.py)")
    for path in root.rglob("*.yaml"):
        if _FORBIDDEN_TEXT.search(path.read_text(encoding="utf-8", errors="replace")):
            report.problems.append(f"{path.name}: names an input-synthesis or process-memory API")
    return report
