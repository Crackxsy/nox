"""The static code scan of a self-built skill: an allow-list, applied to every name and attribute.

This is defence in depth. A skill's worker runs inside the sandbox (`nox.skills.worker_launch`),
which is the boundary; the scan exists so that obviously hostile or careless code is refused
before anyone is asked to switch it on, and so that what the sandbox has to stop is as little as
possible. It is written as an allow-list wherever Python allows one:

* **Imports** - exactly the listed modules (`SRC_IMPORTS`; tests add `unittest.mock`). A listed
  package does not open its submodules, and from the core's own modules only the listed names
  (`PluginApi`, `PluginApiError`, `Risk`) may be taken.
* **Module objects** - a name bound to a module may only be used as `module.attribute`, never
  passed on, stored or aliased. Each attribute of a module is looked up in the real module: it
  must exist, be public, and - when it is itself a module (`asyncio.subprocess`, `typing.sys`,
  `uuid.os`, `json.decoder`) - be one of the allowed modules. Re-exported modules are how the
  plain import list was bypassed (`asyncio.subprocess.subprocess.Popen`).
* **Names and attributes** - nothing that starts with `_` (`random._os`, `__import__`,
  `__builtins__`, `_exit`), except `_` itself and the three dunders ordinary classes need
  (`__init__`, `__name__`, `__doc__`); no keyword argument that starts with `_`. Defining dunder
  methods on one's own classes is fine.
* **Forbidden names and attributes**, wherever they appear and not only when called: the
  builtins that execute, open or reflect (`eval`, `exec`, `compile`, `open`, `getattr`,
  `setattr`, `vars`, `globals`, `locals`, ...), dynamic attribute access in any spelling
  (`operator.attrgetter`, `methodcaller`, `string.Formatter`, `str.format` on a string built at
  runtime or with a `._`/`[_` field), the dangerous module names (`os`, `sys`, `subprocess`,
  `socket`, `ctypes`, `importlib`, `gc`, `inspect`, ...), process and socket entry points
  (`Popen`, `spawnve`, `create_subprocess_exec`, `create_connection`, ...), frames and code
  objects (`f_globals`, `gi_frame`, `tb_frame`, ...), and the string-evaluating helpers
  (`get_type_hints`, `ForwardRef`, pydantic's `ImportString`, `parse_raw`, `allow_pickle`).
* **Strings** - a string that would run code if something evaluated it (a call inside a string
  annotation, which pydantic and `typing` evaluate) is refused, as is any `__dunder__` string.
"""

from __future__ import annotations

import ast
import importlib
import re
import types
from dataclasses import dataclass, field
from typing import Final

__all__ = ["SRC_IMPORTS", "TEST_IMPORTS", "scan_code"]

#: Modules a skill's source may import - exactly these, no submodules unless listed.
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
        "pydantic.dataclasses",
        "pydantic.fields",
        "pydantic.functional_validators",
        "pydantic.functional_serializers",
        "pydantic.types",
        "pydantic.networks",
        "pydantic.config",
        "pydantic.alias_generators",
        "nox.plugins.api",
        "nox.security.model",
    }
)
#: Tests may additionally use mocks. They run in the same locked-down way as the source.
TEST_IMPORTS: Final[frozenset[str]] = SRC_IMPORTS | {"unittest.mock"}

#: From the core's own modules a skill takes exactly these names.
NAMES_FROM: Final[dict[str, frozenset[str]]] = {
    "nox.plugins.api": frozenset({"PluginApi", "PluginApiError"}),
    "nox.security.model": frozenset({"Risk"}),
}

#: Module names that are refused as a bare name or an attribute anywhere (`typing.sys`, `x.os`).
_MODULE_NAMES: Final[frozenset[str]] = frozenset(
    {"os", "sys", "subprocess", "socket", "ctypes", "importlib", "builtins", "shutil", "pathlib",
     "signal", "multiprocessing", "threading", "marshal", "pickle", "winreg", "msvcrt", "posix",
     "nt", "gc", "inspect", "runpy", "pty", "asyncio_subprocess", "windows_utils",
     "base_subprocess", "unix_events", "windows_events", "ssl", "selectors", "mmap"}
)  # fmt: skip
#: Builtins and helpers that execute, open, reflect or reach attributes by a string.
_FORBIDDEN_NAMES: Final[frozenset[str]] = frozenset(
    {"eval", "exec", "compile", "open", "input", "breakpoint", "globals", "locals", "vars",
     "memoryview", "help", "exit", "quit", "getattr", "setattr", "delattr", "hasattr",
     "attrgetter", "methodcaller", "get_type_hints", "ForwardRef", "evaluate_forward_ref",
     "Formatter", "ImportString", "patch", "SystemExit", "BaseException", "KeyboardInterrupt",
     "GeneratorExit"}
)  # fmt: skip
#: Attributes that reach processes, sockets, files, frames or string evaluation.
_FORBIDDEN_ATTRS: Final[frozenset[str]] = frozenset(
    {"system", "popen", "Popen", "startfile", "fork", "forkpty", "kill", "killpg",
     "spawnv", "spawnve", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnvp", "spawnvpe",
     "posix_spawn", "posix_spawnp", "execv", "execve", "execl", "execle", "execlp", "execlpe",
     "execvp", "execvpe", "create_subprocess_exec", "create_subprocess_shell", "subprocess_exec",
     "subprocess_shell", "open_connection", "open_unix_connection", "start_server",
     "start_unix_server", "create_connection", "create_datagram_endpoint", "create_server",
     "create_unix_connection", "create_unix_server", "connect_read_pipe", "connect_write_pipe",
     "connect_accepted_socket", "getaddrinfo", "sock_connect", "sock_sendall", "sock_recv",
     "sock_accept", "sock_sendto", "sock_recvfrom", "sock_sendfile", "sendfile", "parse_file",
     "parse_raw", "parse_file_as", "parse_raw_as", "add_signal_handler", "get_stack",
     "print_stack", "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code",
     "tb_frame", "tb_next", "f_globals", "f_locals", "f_builtins", "f_back", "f_code", "co_code",
     "modules", "getter", "get_field", "load_module", "exec_module", "run_in_executor",
     "set_default_executor", "loader"}
)  # fmt: skip
_FORBIDDEN_KEYWORDS: Final[frozenset[str]] = frozenset({"allow_pickle"})
_ALLOWED_DUNDERS: Final[frozenset[str]] = frozenset({"__init__", "__name__", "__doc__"})
_DUNDER_STRING = re.compile(r"__\w+__")
#: A `str.format` field that reaches a private attribute or item (`"{0._os}"`, `"{0[_x]}"`).
_PRIVATE_FIELD = re.compile(r"\{[^{}]*?[.\[]\s*_")
#: CI's list (Security Model §10) plus the other input-synthesis entry points. Spelled in pieces:
#: the CI scan cannot tell this list from a use of what it names (see `nox.desktop.keyboard`).
FORBIDDEN_TEXT = re.compile(
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
            ("SetCursor", "Pos"),
            ("SetWindows", "HookEx"),
            ("CreateRemote", "Thread"),
            ("Virtual", "AllocEx"),
        )
    ),
    re.IGNORECASE,
)


def module_allowed(module: str, allowed: frozenset[str], own: str) -> bool:
    """`module` is the skill's own package or one of the listed modules, exactly."""
    return module == own or module.startswith(f"{own}.") or module in allowed


def _load(name: str) -> types.ModuleType | None:
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - a module the core cannot load cannot be checked: refused
        return None


def _private(name: str) -> bool:
    return name.startswith("_") and name != "_" and name not in _ALLOWED_DUNDERS


@dataclass
class _Scan:
    filename: str
    own: str
    allowed: frozenset[str]
    problems: list[str] = field(default_factory=list)
    #: Names bound to a module object -> the module's dotted name.
    modules: dict[str, str] = field(default_factory=dict)
    #: ids of Name nodes that are the base of an attribute access.
    attribute_bases: set[int] = field(default_factory=set)

    def refuse(self, node: ast.AST, text: str) -> None:
        self.problems.append(f"{self.filename}:{getattr(node, 'lineno', 0)}: {text}")

    # -- modules -----------------------------------------------------------------------------

    def _prefix_only(self, dotted: str) -> bool:
        """A package that is not allowed itself but leads to one that is (`email`, `nox`)."""
        return dotted not in self.allowed and any(a.startswith(f"{dotted}.") for a in self.allowed)

    def _module_reachable(self, dotted: str) -> bool:
        return dotted in self.allowed or self._prefix_only(dotted)

    def _member(self, node: ast.AST, dotted: str, name: str) -> types.ModuleType | None:
        """Check `dotted.name`; return the member when it is itself a module."""
        restricted = NAMES_FROM.get(dotted)
        if restricted is not None and name not in restricted:
            self.refuse(node, f"{dotted}.{name} is not available to a skill")
            return None
        module = _load(dotted)
        if module is None:
            self.refuse(node, f"{dotted} cannot be checked")
            return None
        if f"{dotted}.{name}" in self.allowed or self._prefix_only(f"{dotted}.{name}"):
            submodule = _load(f"{dotted}.{name}")
            if submodule is not None:
                return submodule
        if not hasattr(module, name):
            self.refuse(node, f"{dotted} has no public {name}")
            return None
        member = getattr(module, name)
        if isinstance(member, types.ModuleType):
            if not self._module_reachable(member.__name__):
                self.refuse(node, f"{dotted}.{name} is the module {member.__name__}")
                return None
            return member
        if self._prefix_only(dotted):
            self.refuse(node, f"only the listed modules of {dotted} are available to a skill")
        return None

    def collect_imports(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not module_allowed(alias.name, self.allowed, self.own):
                        self.refuse(node, f"import {alias.name} is not allowed in a skill")
                        continue
                    if alias.asname:
                        self.modules[alias.asname] = alias.name
                    else:
                        root = alias.name.split(".", 1)[0]
                        self.modules[root] = root
            elif isinstance(node, ast.ImportFrom):
                self._import_from(node)

    def _import_from(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        own = bool(node.level) or module == self.own or module.startswith(f"{self.own}.")
        if not own and not module_allowed(module, self.allowed, self.own):
            self.refuse(node, f"import from {module} is not allowed in a skill")
            return
        for alias in node.names:
            if alias.name == "*":
                self.refuse(node, "import * is not allowed in a skill")
                continue
            if own or module == "__future__":
                continue
            member = self._member(node, module, alias.name)
            if member is not None:
                self.modules[alias.asname or alias.name] = member.__name__

    # -- every node ---------------------------------------------------------------------------

    def _static_module(self, node: ast.AST) -> str | None:
        """The dotted name of the module `node` evaluates to, when that is known statically."""
        if isinstance(node, ast.Name):
            return self.modules.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._static_module(node.value)
            if base is None:
                return None
            dotted = f"{base}.{node.attr}"
            if dotted in self.allowed or self._prefix_only(dotted):
                return dotted
            module = _load(base)
            member = getattr(module, node.attr, None) if module is not None else None
            if isinstance(member, types.ModuleType):
                return member.__name__
        return None

    def visit(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                self.attribute_bases.add(id(node.value))
        for node in ast.walk(tree):
            self._node(node)

    def _node(self, node: ast.AST) -> None:  # noqa: C901, PLR0912 - one flat rule table
        if isinstance(node, ast.Name):
            self._name(node, node.id)
            if node.id in self.modules and id(node) not in self.attribute_bases:
                self.refuse(node, f"the module {node.id} may only be used as {node.id}.name")
        elif isinstance(node, ast.Attribute):
            self._attribute(node)
        elif isinstance(node, ast.alias):
            for name in (node.name.rsplit(".", 1)[-1], node.asname or ""):
                if name and (_private(name) or name in _FORBIDDEN_NAMES):
                    self.refuse(node, f"{name} is not allowed in a skill")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            dunder = node.name.startswith("__") and node.name.endswith("__")
            if _private(node.name) and not dunder:
                self.refuse(node, f"{node.name}: names starting with _ are not allowed in a skill")
        elif isinstance(node, ast.arg):
            if _private(node.arg):
                self.refuse(node, f"{node.arg}: names starting with _ are not allowed in a skill")
        elif isinstance(node, ast.keyword):
            if node.arg is not None and (_private(node.arg) or node.arg in _FORBIDDEN_KEYWORDS):
                self.refuse(node, f"keyword {node.arg} is not allowed in a skill")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                self._name(node, name)
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("format", "format_map")
                and not (isinstance(func.value, ast.Constant) and isinstance(func.value.value, str))
            ):
                self.refuse(node, "str.format is allowed only on a literal format string")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            self._string(node, node.value)

    def _name(self, node: ast.AST, name: str) -> None:
        if _private(name):
            self.refuse(node, f"{name}: names starting with _ are not allowed in a skill")
        elif name in _FORBIDDEN_NAMES or name in _MODULE_NAMES:
            self.refuse(node, f"{name} is not allowed in a skill")

    def _attribute(self, node: ast.Attribute) -> None:
        attr = node.attr
        if _private(attr):
            self.refuse(node, f"attribute {attr} is not allowed in a skill")
            return
        if attr in _FORBIDDEN_ATTRS or attr in _FORBIDDEN_NAMES or attr in _MODULE_NAMES:
            self.refuse(node, f".{attr} is not allowed in a skill")
            return
        base = self._static_module(node.value)
        if base is not None:
            self._member(node, base, attr)

    def _string(self, node: ast.Constant, text: str) -> None:
        if _DUNDER_STRING.fullmatch(text.strip()):
            self.refuse(node, f"the string {text.strip()!r} is not allowed in a skill")
            return
        if _PRIVATE_FIELD.search(text):
            self.refuse(node, "a format field that reaches a private attribute")
            return
        if len(text) > 2000 or "(" not in text:
            return
        try:
            expression = ast.parse(text.strip(), mode="eval")
        except (SyntaxError, ValueError):
            return
        if any(isinstance(n, (ast.Call, ast.Lambda)) for n in ast.walk(expression)):
            # pydantic and typing evaluate string annotations; a call in one would run.
            self.refuse(node, "a string that would run code if it were evaluated")


def scan_code(source: str, *, filename: str, own_package: str, tests: bool = False) -> list[str]:
    """Problems in one Python file; empty when it only uses what a skill may use."""
    problems: list[str] = []
    if FORBIDDEN_TEXT.search(source):
        problems.append(f"{filename}: names an input-synthesis or process-memory API")
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [*problems, f"{filename}: syntax error on line {exc.lineno}: {exc.msg}"]
    scan = _Scan(filename, own_package, TEST_IMPORTS if tests else SRC_IMPORTS)
    scan.collect_imports(tree)
    scan.visit(tree)
    for message in scan.problems:
        if message not in problems:
            problems.append(message)
    return problems
