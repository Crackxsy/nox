"""Build `nox-app-<tag>.zip`, the bundle a `nox node` updates itself from (`nox.node.update`).

    python scripts/build_app_bundle.py --tag v0.7.1 --out files/

Standard library only (the release workflow runs it before anything is installed). The bundle holds
Nox's own code and configuration - `nox/` (from `src/nox`) and `config/` - and `BUNDLE.json`
(`nox.node.bundle`): the version, the node protocol this code speaks, the SHA-256 of the
`uv.lock` it belongs to (a node applies it only on exactly those libraries) and the SHA-256 of
every file (checked when unpacking and again at every start). No libraries, no compiled code:
those come only with an installer.

Deterministic: sorted entries, fixed timestamps and modes, so two builds of one tag are identical
and the SHA-256 in the signed release manifest describes exactly this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
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


def node_protocol_of(repo: Path) -> int:
    """`NODE_PROTOCOL` of this checkout: the protocol its `nox node` speaks."""
    text = (repo / "src" / "nox" / "node" / "protocol.py").read_text(encoding="utf-8")
    match = re.search(r"^NODE_PROTOCOL = (\d+)$", text, re.MULTILINE)
    if match is None:
        raise SystemExit("src/nox/node/protocol.py has no NODE_PROTOCOL")
    return int(match[1])


def lock_sha256(repo: Path) -> str:
    """SHA-256 of `uv.lock`: a node applies the bundle only on exactly these libraries."""
    return hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest()


def bundle_info(repo: Path, version: str, files: list[tuple[str, Path]]) -> dict[str, object]:
    return {
        "schema": SCHEMA,
        "version": version,
        "node_protocol": node_protocol_of(repo),
        "lock_sha256": lock_sha256(repo),
        "files": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files},
    }


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
    files = _files(repo)
    info = json.dumps(bundle_info(repo, version, files), indent=2, sort_keys=True) + "\n"
    with zipfile.ZipFile(target, "w") as bundle:
        bundle.writestr(_entry("BUNDLE.json"), info)
        for name, path in files:
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
