"""Build `nox-app-<tag>.zip`, the bundle a `nox node` updates itself from (`nox.node.update`).

    python scripts/build_app_bundle.py --tag v0.7.1 --out files/

Standard library only (the release workflow runs it before anything is installed). The bundle holds
Nox's own code and configuration - `nox/` (from `src/nox`) and `config/` - and `BUNDLE.json`: the
version, and the exact versions of every package in `uv.lock`, so a node can tell whether the
libraries it has fit this code (`packages`) and which ones it must have (`requires`, the base
dependencies). No libraries, no compiled code: those come only with an installer.

Deterministic: sorted entries, fixed timestamps and modes, so two builds of one tag are identical
and the SHA-256 in the signed release manifest describes exactly this file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCHEMA = "nox-app-bundle/1"
_STAMP = (1980, 1, 1, 0, 0, 0)
_SKIP_DIRS = {"__pycache__"}
_SKIP_SUFFIXES = {".pyc", ".pyo"}


def version_of(repo: Path) -> str:
    text = (repo / "src" / "nox" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise SystemExit("src/nox/__init__.py has no __version__")
    return match[1]


def bundle_info(repo: Path, version: str) -> dict[str, object]:
    lock = tomllib.loads((repo / "uv.lock").read_text(encoding="utf-8"))
    # A lock can hold one package in two versions (for different platforms): all are listed.
    packages: dict[str, list[str]] = {}
    for package in lock.get("package", []):
        if package.get("name") != "nox" and "version" in package:
            versions = packages.setdefault(str(package["name"]), [])
            if str(package["version"]) not in versions:
                versions.append(str(package["version"]))
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requires = sorted(
        re.split(r"[\s<>=!~;\[]", dep, maxsplit=1)[0] for dep in project["dependencies"]
    )
    return {"schema": SCHEMA, "version": version, "packages": packages, "requires": requires}


def _files(repo: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for prefix, source in (("nox", repo / "src" / "nox"), ("config", repo / "config")):
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if path.is_symlink():
                raise SystemExit(f"refusing to bundle a link: {path}")
            if not path.is_file() or _SKIP_DIRS & set(relative.parts):
                continue
            if path.suffix in _SKIP_SUFFIXES:
                continue
            found.append((f"{prefix}/{relative.as_posix()}", path))
    return found


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_STAMP)
    info.external_attr = 0o100644 << 16  # a regular file, rw-r--r--
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build(repo: Path, tag: str, out: Path) -> Path:
    version = version_of(repo)
    if tag != f"v{version}":
        raise SystemExit(f"tag {tag} does not match src/nox/__init__.py ({version})")
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"nox-app-{tag}.zip"
    info = json.dumps(bundle_info(repo, version), indent=2, sort_keys=True) + "\n"
    with zipfile.ZipFile(target, "w") as bundle:
        bundle.writestr(_entry("BUNDLE.json"), info)
        for name, path in _files(repo):
            bundle.writestr(_entry(name), path.read_bytes())
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=REPO)
    args = parser.parse_args()
    print(build(args.repo, args.tag, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
