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
* **Code** - `nox.skills.scan`: an allow-list of imports, of module attributes and of names, so
  nothing reaches processes, files, the network, the registry, the keyring or the interpreter
  itself - not by import, not through a module another module re-exports, not through a `_`
  name, not by a string. The network exists only as `api.http()`, which the worker scopes to the
  manifest's egress. The input-synthesis and process-memory APIs CI forbids in Nox are refused.
  The scan is defence in depth: the skill's worker and its tests run only inside the sandbox.

`tree_hash()` is what activation is bound to: the files the user was shown are the files that run.
"""

from __future__ import annotations

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
from nox.skills.scan import FORBIDDEN_TEXT, SRC_IMPORTS, TEST_IMPORTS, scan_code

__all__ = [
    "SRC_IMPORTS",
    "TEST_IMPORTS",
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
        if FORBIDDEN_TEXT.search(path.read_text(encoding="utf-8", errors="replace")):
            report.problems.append(f"{path.name}: names an input-synthesis or process-memory API")
    return report
