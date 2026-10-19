"""What an external MCP server may be: the checks run before one is started or its tools appear.

An MCP server is someone else's code. Nox runs it, hands it arguments a language model chose, and
shows its tool names and descriptions to that model. Every rule below closes one way that goes
wrong, and each one is applied in code before anything is spawned or registered:

* **Pinned code.** `npx`, `uvx` and friends download whatever a package name points to *today*.
  A server started through one of them must name an exact version in its command, and that
  version must be the one the user pinned - otherwise tomorrow's start runs code nobody approved.
* **No shell in between.** `cmd /c ...` or `bash -c ...` hides what actually runs from the
  approval dialog, so a shell is never the program.
* **Secrets by reference only.** An environment value or header that looks like a credential must
  be `secret:nox/mcp/<id>/<name>`, resolved from the secret store at start; the config file, the
  dashboard and the audit log never carry the value.
* **No hijacked environment.** A server cannot set variables that change which code its own
  interpreter loads (`NODE_OPTIONS`, `PYTHONPATH`, `LD_PRELOAD`, ...) or Nox's own.
* **Remote means HTTPS**, through the egress guard, never to the Home Assistant host.
* **Names.** Server ids and tool names are checked against the hard prohibitions, the reserved
  namespaces and the home boundary (`nox.security.reserved`), and mapped to `mcp.<id>.<tool>`
  within the 64-character wire limit.

The first start of a server - and every start after its command changed - is approved by the user
in the dashboard; `fingerprint()` is what that approval is bound to.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePath
from typing import Any, Final
from urllib.parse import parse_qsl, urlsplit

from nox.core.config.hands import McpServerConfig
from nox.security.egress import is_loopback
from nox.security.reserved import name_refusal, untrusted_text
from nox.security.secrets import SECRET_NAME_RE
from nox.tools.names import WIRE_NAME_RE

__all__ = [
    "SECRET_REF_PREFIX",
    "PolicyError",
    "check_server",
    "fingerprint",
    "nox_tool_name",
    "secret_refs",
]

SECRET_REF_PREFIX = "secret:"  # noqa: S105 - a reference prefix, not a value

#: Programs that fetch a package at start, and how each one spells "this exact version".
_NPM_RUNNERS: Final[frozenset[str]] = frozenset({"npx", "pnpx", "bunx"})
_PY_RUNNERS: Final[frozenset[str]] = frozenset({"uvx"})
_SUBCOMMAND_RUNNERS: Final[dict[str, str]] = {"pipx": "run", "pnpm": "dlx", "yarn": "dlx"}
_SHELLS: Final[frozenset[str]] = frozenset(
    {"cmd", "powershell", "pwsh", "bash", "sh", "zsh", "dash", "fish", "wsl", "start"}
)

#: npm `name@1.2.3` / `@scope/name@1.2.3-beta.1` - an exact version, no range, no tag.
_NPM_PINNED = re.compile(
    r"^(?P<name>(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*)"
    r"@(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$"
)
#: PyPI `name==1.2.3`, `name[extra]==1.2.3` or uv's `name@1.2.3`.
_PY_PINNED = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?)"
    r"(?:==|@)(?P<version>\d+(?:\.\d+)*(?:[a-z]+\d*)?(?:\.post\d+)?(?:\.dev\d+)?)$"
)
#: npx options that take the package as their value.
_NPM_PACKAGE_OPTIONS: Final[frozenset[str]] = frozenset({"-p", "--package"})
#: uvx options that name a package to install.
_PY_PACKAGE_OPTIONS: Final[frozenset[str]] = frozenset({"--from", "--with"})
#: uvx options that take a value which is not a package.
_PY_VALUE_OPTIONS: Final[frozenset[str]] = frozenset(
    {"--python", "-p", "--index", "--index-url", "--extra-index-url", "--default-index"}
)

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HEADER_KEY = re.compile(r"^[A-Za-z0-9-]{1,64}$")
#: Variables that decide which code an interpreter loads, or that belong to Nox itself.
_FORBIDDEN_ENV: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "NODE_OPTIONS",
        "NODE_PATH",
        "NODE_EXTRA_CA_CERTS",
        "NPM_CONFIG_REGISTRY",
        "NPM_CONFIG_PREFIX",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "PYTHONUSERBASE",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "UV_DEFAULT_INDEX",
        "PIP_INDEX_URL",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    }
)
_FORBIDDEN_ENV_PREFIXES: Final[tuple[str, ...]] = ("DYLD_", "NOX_", "LD_")
#: Name fragments of a value that has to come from the secret store.
_SECRETISH = re.compile(
    r"(TOKEN|SECRET|PASSW|PASSPHRASE|API_?KEY|ACCESS_?KEY|PRIVATE|CREDENTIAL|AUTH|COOKIE|SESSION|"
    r"BEARER|(^|_)PAT($|_)|(^|_)KEY($|_))",
    re.IGNORECASE,
)
#: Values that are recognisably a credential whatever their key is called.
_CREDENTIAL_SHAPES = re.compile(
    r"^(ghp_|gho_|ghs_|github_pat_|glpat-|sk-|sk_live_|rk_live_|xox[abpr]-|AKIA|AIza|ya29\.|"
    r"Bearer\s|Basic\s)",
)

#: Longest tool-name part in `mcp.<id>.<tool>`; the wire form `mcp-<id>-<tool>` must fit in 64.
_WIRE_LIMIT = 64
_TOOL_TOKEN = re.compile(r"[^a-z0-9_]+")


class PolicyError(ValueError):
    """A server configuration Nox will not start, with the reason in words."""


# ---- names ---------------------------------------------------------------------------------------


def check_server_id(server_id: str) -> None:
    refusal = name_refusal(server_id)
    if refusal is not None:
        raise PolicyError(f"server id {server_id!r} {refusal}")


def nox_tool_name(server_id: str, remote_name: str) -> str:
    """`mcp.<server>.<tool>` for a tool a server calls `remote_name`, or `PolicyError`.

    The remote name is lower-cased and every character outside `[a-z0-9_]` becomes `_`, so the
    result is a valid Nox name (no `-`, which is the wire form of `.`) and a valid wire name. The
    *remote* name is what is checked against the boundaries, before and after that mapping, so a
    server cannot slip `game.input.send` past them as `game-input-send`.
    """
    for candidate in (remote_name, remote_name.replace("-", ".")):
        refusal = name_refusal(candidate)
        if refusal is not None:
            raise PolicyError(f"tool {untrusted_text(remote_name, 64)!r} {refusal}")
    tool = _TOOL_TOKEN.sub("_", remote_name.strip().lower()).strip("_")
    tool = re.sub(r"_{2,}", "_", tool)
    if not tool:
        raise PolicyError("a tool without a usable name")
    if tool[0].isdigit():
        tool = f"t_{tool}"
    room = _WIRE_LIMIT - len("mcp--") - len(server_id)
    if len(tool) > room:
        raise PolicyError(f"tool name {tool[:24]!r}... is too long for the server id {server_id!r}")
    refusal = name_refusal(tool)
    if refusal is not None:
        raise PolicyError(f"tool {tool!r} {refusal}")
    name = f"mcp.{server_id}.{tool}"
    if not WIRE_NAME_RE.fullmatch(name.replace(".", "-")):
        raise PolicyError(f"tool {tool!r} cannot be expressed as a wire name")
    return name


# ---- command -------------------------------------------------------------------------------------


def _program(argv0: str) -> str:
    """`C:\\...\\npx.cmd` -> `npx`; the program a command line starts, lower-case, no suffix."""
    name = PurePath(argv0.replace("\\", "/")).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _npm_packages(args: list[str]) -> list[str]:
    packages: list[str] = []
    index = 0
    positional: str | None = None
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        if arg in _NPM_PACKAGE_OPTIONS and index + 1 < len(args):
            packages.append(args[index + 1])
            index += 2
            continue
        if arg.startswith("--package="):
            packages.append(arg.split("=", 1)[1])
        elif not arg.startswith("-") and positional is None:
            positional = arg
            break
        index += 1
    if not packages and positional is not None:
        packages.append(positional)
    return packages


def _py_packages(args: list[str]) -> list[str]:
    packages: list[str] = []
    named_from = False
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        option, _, inline = arg.partition("=")
        if option in _PY_PACKAGE_OPTIONS:
            value = inline or (args[index + 1] if index + 1 < len(args) else "")
            packages.append(value)
            named_from = named_from or option == "--from"
            index += 1 if inline else 2
            continue
        if option in _PY_VALUE_OPTIONS:
            index += 1 if inline else 2
            continue
        if not arg.startswith("-"):
            # With `--from`, the first positional is the command to run, not a package.
            if not named_from:
                packages.append(arg)
            break
        index += 1
    return packages


def _check_pinned(program: str, args: list[str], version: str) -> None:
    if program in _NPM_RUNNERS:
        packages, pattern, example = _npm_packages(args), _NPM_PINNED, "name@1.2.3"
    else:
        packages, pattern, example = _py_packages(args), _PY_PINNED, "name==1.2.3"
    if not packages:
        raise PolicyError(f"{program} without a package to run")
    versions: list[str] = []
    for package in packages:
        match = pattern.fullmatch(package)
        if match is None:
            raise PolicyError(
                f"{program} runs {untrusted_text(package, 80)!r} without an exact version; "
                f"pin it ({example})"
            )
        versions.append(match.group("version"))
    if not version:
        raise PolicyError(f"a server started through {program} needs its pinned version set")
    if version not in versions:
        raise PolicyError(
            f"the command runs version {versions[0]}, but {version} is the one pinned for it"
        )


def check_command(command: list[str], version: str) -> None:
    if not command or not command[0].strip():
        raise PolicyError("the command is empty")
    if any("\x00" in part or "\n" in part for part in command):
        raise PolicyError("the command contains a control character")
    program = _program(command[0])
    if program in _SHELLS:
        raise PolicyError(
            f"{program} would hide what actually runs; name the server program itself"
        )
    args = command[1:]
    if program in _NPM_RUNNERS or program in _PY_RUNNERS:
        _check_pinned(program, args, version)
    elif program in _SUBCOMMAND_RUNNERS:
        sub = _SUBCOMMAND_RUNNERS[program]
        if not args or args[0] != sub:
            raise PolicyError(f"{program} may only start a server as `{program} {sub} ...`")
        runner = "npx" if program in ("pnpm", "yarn") else "uvx"
        _check_pinned(runner, args[1:], version)


# ---- environment, headers, url -------------------------------------------------------------------


def _check_value(server_id: str, key: str, value: str, *, what: str) -> None:
    if value.startswith(SECRET_REF_PREFIX):
        name = value[len(SECRET_REF_PREFIX) :]
        if not SECRET_NAME_RE.match(name) or not name.startswith(f"nox/mcp/{server_id}/"):
            raise PolicyError(
                f"{what} {key}: a secret reference must be secret:nox/mcp/{server_id}/<name>"
            )
        return
    if "\x00" in value or "\n" in value or "\r" in value:
        raise PolicyError(f"{what} {key} contains a control character")
    if _SECRETISH.search(key) or _CREDENTIAL_SHAPES.match(value.strip()):
        raise PolicyError(
            f"{what} {key} looks like a credential; store it as a secret and use "
            f"secret:nox/mcp/{server_id}/<name>"
        )


def check_env(server_id: str, env: dict[str, str]) -> None:
    for key, value in env.items():
        if not _ENV_KEY.fullmatch(key):
            raise PolicyError(f"environment variable {key!r} is not a valid name")
        upper = key.upper()
        if upper in _FORBIDDEN_ENV or upper.startswith(_FORBIDDEN_ENV_PREFIXES):
            raise PolicyError(f"environment variable {key} may not be set for an MCP server")
        _check_value(server_id, key, value, what="environment variable")


def check_headers(server_id: str, headers: dict[str, str]) -> None:
    for key, value in headers.items():
        if not _HEADER_KEY.fullmatch(key):
            raise PolicyError(f"header {key!r} is not a valid name")
        if key.lower() in ("host", "content-length", "transfer-encoding", "mcp-session-id"):
            raise PolicyError(f"header {key} is set by Nox, not by the configuration")
        _check_value(server_id, key, value, what="header")


def check_url(url: str, *, home_host: str = "") -> None:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host:
        raise PolicyError("a remote MCP server needs an https:// URL")
    if parts.scheme == "http" and not is_loopback(host):
        raise PolicyError("plain http is only allowed to this machine; use https://")
    if parts.username or parts.password:
        raise PolicyError("credentials do not belong in the URL; use a header with a secret")
    if home_host and host == home_host.strip().lower():
        raise PolicyError("the Home Assistant host is not reachable through an MCP server")
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if _SECRETISH.search(key) or _CREDENTIAL_SHAPES.match(value):
            raise PolicyError(f"query parameter {key} looks like a credential; use a header")


def check_server(server: McpServerConfig, *, home_host: str = "") -> None:
    """Every rule above for one server; raises `PolicyError` with the first that fails."""
    check_server_id(server.id)
    if server.command:
        check_command(server.command, server.version.strip())
        check_env(server.id, server.env)
        if server.headers:
            raise PolicyError("headers only apply to a remote server")
    else:
        check_url(server.url, home_host=home_host)
        check_headers(server.id, server.headers)
        if server.env:
            raise PolicyError("environment variables only apply to a local server")
    for tool in server.read_only_tools:
        nox_tool_name(server.id, tool)


def secret_refs(values: dict[str, str]) -> dict[str, str]:
    """`{key: secret name}` for every value that is a secret reference."""
    return {
        key: value[len(SECRET_REF_PREFIX) :]
        for key, value in values.items()
        if value.startswith(SECRET_REF_PREFIX)
    }


def fingerprint(server: McpServerConfig) -> str:
    """What a user's approval of a server is bound to: everything that decides what runs."""
    material: dict[str, Any] = {
        "command": list(server.command),
        "url": server.url.strip(),
        "version": server.version.strip(),
        "env": dict(sorted(server.env.items())),
        "headers": dict(sorted(server.headers.items())),
        "network": server.network,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
