"""`package.install` / `package.remove`: install software from an official source, with a confirm.

Unlike `shell.run` these are *not* sandboxed - installing software changes the system by design.
What keeps them safe is the gate above them: HIGH risk, always CONFIRM, and `rememberable=False`,
so there is never an "always allow" for installing software. The source is pinned: `winget` only
from the `winget` source (no arbitrary msstore or URL), and on Linux/Pi only `apt-get install` /
`remove -y <pkg>` through the one narrow sudoers rule the installer adds. The package id is
validated against a conservative pattern, so nothing with a shell metacharacter reaches the argv.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, field_validator

from nox.core.logging import get_logger
from nox.security.model import Risk
from nox.tools.registry import ToolRegistry, ToolSpec

log = get_logger(__name__)

__all__ = ["PackageResult", "RunPackage", "register_package_tools", "winget_argv", "apt_argv"]

#: A package id: letters, digits and the few punctuation marks real ids use. No spaces, no shell
#: metacharacters - so the id can never smuggle a second command into the fixed argv.
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class PackageResult(BaseModel):
    ok: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


#: Runs a fixed argv and returns (exit_code, stdout, stderr). Injected, so tests use a fake and the
#: real one is an `asyncio` subprocess with no shell.
RunPackage = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


def winget_argv(action: str, package_id: str) -> list[str]:
    verb = "install" if action == "install" else "uninstall"
    argv = [
        "winget",
        verb,
        "--source",
        "winget",
        "--id",
        package_id,
        "--exact",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    if action == "install":
        argv.append("--accept-package-agreements")
    return argv


def apt_argv(action: str, package_id: str) -> list[str]:
    verb = "install" if action == "install" else "remove"
    # Exactly the shape the installer's sudoers rule permits: `apt-get install/remove -y <pkg>`.
    return ["sudo", "-n", "apt-get", verb, "-y", package_id]


class PackageInput(BaseModel):
    package_id: str

    @field_validator("package_id")
    @classmethod
    def _valid(cls, value: str) -> str:
        value = value.strip()
        if not _PACKAGE_ID.match(value):
            raise ValueError("package id has an unexpected character")
        return value


def register_package_tools(registry: ToolRegistry, run: RunPackage) -> None:
    def _argv(action: str, package_id: str) -> list[str]:
        if sys.platform == "win32":
            return winget_argv(action, package_id)
        return apt_argv(action, package_id)

    def _make(action: str) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
        async def handler(payload: dict[str, Any]) -> dict[str, Any]:
            model = PackageInput.model_validate(payload)
            argv = _argv(action, model.package_id)
            try:
                code, out, err = await run(argv)
            except OSError as exc:
                return PackageResult(ok=False, error=f"{type(exc).__name__}: {exc}").model_dump()
            return PackageResult(
                ok=code == 0, exit_code=code, stdout=out[:8192], stderr=err[:8192]
            ).model_dump()

        return handler

    for action, verb in (("install", "Install"), ("remove", "Remove")):
        registry.register(
            ToolSpec(
                name=f"package.{action}",
                description=f"{verb} a program by its official package id "
                "(winget source 'winget' on Windows, apt on Linux/Pi). Always asks first.",
                input_model=PackageInput,
                risk=Risk.HIGH,
                handler=_make(action),
                side_effects=True,
                local=True,
                rememberable=False,
                targets=lambda p: str(p.get("package_id", "")),
            )
        )
