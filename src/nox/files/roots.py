"""The boundary: turning a path a model wrote into a path that is inside a configured root.

Everything the file tools do goes through `resolve_inside`. It is deliberately the only way in, and
it is deliberately paranoid, because the input comes from a language model and the output is handed
to the filesystem.

* **Resolved first, checked second.** `Path.resolve()` follows `..`, junctions and symlinks, so the
  check is against where the path really leads, not what it looks like. Checking the text first and
  resolving afterwards is the classic way to let `roots/../../Windows` through.
* **An empty root list means nowhere.** Not "everywhere" - that inversion is how a guard meant to
  restrict ends up permitting, and this project has one of those already: a profile's
  `filesystem_roots` is skipped when empty, which is fine there because it is a *narrowing* on top
  of this list, and reads oddly until you know that.
* **The error names the setting.** "Outside the folders you configured (files.roots)" is something a
  user can act on. "Permission denied" is not.
"""

from __future__ import annotations

from pathlib import Path

from nox.core.config.types import expand_path

__all__ = ["OutsideRootsError", "resolve_inside", "resolved_roots"]


class OutsideRootsError(Exception):
    """A path that does not lead inside any configured root. Raised before any I/O happens."""


def resolved_roots(roots: list[str]) -> list[Path]:
    """The configured roots, expanded and resolved, skipping the ones that do not exist.

    A root that is not there yet is not an error - an external drive can be unplugged - but it is
    also not a root, because a path "inside" a missing folder cannot be checked against anything.
    """
    out: list[Path] = []
    for entry in roots:
        try:
            resolved = expand_path(entry).resolve()
        except (OSError, ValueError):
            continue
        if resolved.is_dir():
            out.append(resolved)
    return out


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def resolve_inside(candidate: str, roots: list[str], *, must_exist: bool = False) -> Path:
    """The resolved path, guaranteed to be inside one configured root.

    `must_exist` is for reads and moves-from; a write or a move-to may name something that is not
    there yet. Either way the *parent* has to resolve inside a root, which is what stops a write
    from creating a file through a junction that leads out.
    """
    available = resolved_roots(roots)
    if not available:
        raise OutsideRootsError(
            "no folder is configured for file access; add one to files.roots in the settings"
        )
    text = candidate.strip()
    if not text:
        raise OutsideRootsError("no path was given")
    try:
        # `strict=False`: a path that does not exist yet still resolves, and its parents are what
        # the containment check needs.
        path = expand_path(text).resolve()
    except (OSError, ValueError) as exc:
        raise OutsideRootsError(f"{candidate!r} is not a usable path") from exc

    if not any(_inside(path, root) for root in available):
        names = ", ".join(str(root) for root in available)
        raise OutsideRootsError(f"{path} is outside the folders you configured ({names})")
    if must_exist and not path.exists():
        raise OutsideRootsError(f"{path} does not exist")
    return path
