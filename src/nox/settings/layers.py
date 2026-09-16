"""User-layer (layer 2, `user.yaml`) read/write helpers shared by `nox onboard` and the dashboard.

ADR-010's four layers are Defaults -> User -> Profile/Preset -> Runtime Override. Both writers
that exist - the onboarding wizard and `config.set` - must touch exactly the same file, in the
same way, or the two would silently disagree about where a setting lives. The implementation
therefore lives here and `nox.onboarding.wizard` re-exports it.

The document is round-tripped through `ruamel.yaml` (#22): comments, key order, quoting style and
unknown keys all survive a write, and only the keys named in the patch change. What lands on disk
is plain YAML - every reader (`nox.core.config`, `load_existing_user_layer` below) still parses it
with `yaml.safe_load`, and a file this module has never seen is adopted as it is written.
"""

from __future__ import annotations

import io
import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any, cast

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from nox.core.config import NoxConfig, load_config

#: `<repo>/config/defaults.yaml` - resolved from this module, not from the working directory, so
#: a plugin worker or a test with a different cwd finds the same file the core uses.
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULTS_PATH = REPO_ROOT / "config" / "defaults.yaml"

USER_CONFIG_HEADER = (
    "# Nox user configuration - layer 2 of 4 (Defaults -> User -> Profile -> Runtime).\n"
    "# Written by `nox onboard` and by the dashboard's Settings page (`config.set`).\n"
    "# Never edited by the Defaults layer; never contains secrets (those live in the OS keyring).\n"
    "# Your own comments and keys are kept when Nox writes this file.\n"
)


class UserConfigError(ValueError):
    """`user.yaml` exists but does not parse as YAML.

    Raised instead of writing: a syntax error in a hand-edited file must not cost the user the
    rest of the file, and it must not be silently "fixed" by overwriting it with a fresh document.
    """


def default_user_config_path(appdata: Path | str | None = None) -> Path:
    """`%APPDATA%\\Nox\\user.yaml` - the same default `nox.app.build_config` falls back to."""
    base = Path(appdata) if appdata is not None else Path(os.environ["APPDATA"])
    return base / "Nox" / "user.yaml"


def resolve_defaults_path() -> Path:
    """The Defaults layer the running core uses (`NOX_CONFIG_DEFAULTS` wins, as in `nox.app`)."""
    return Path(os.environ.get("NOX_CONFIG_DEFAULTS", DEFAULTS_PATH))


def resolve_user_config_path() -> Path:
    """Where `config.set` writes: `NOX_USER_CONFIG` if set, else `%APPDATA%\\Nox\\user.yaml`.

    Unlike `nox.app.build_config` this returns the path even when the file does not exist yet -
    the point of a writer is to create it.
    """
    env_user = os.environ.get("NOX_USER_CONFIG")
    if env_user:
        return Path(env_user)
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Nox" / "user.yaml"


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Nested dicts merged recursively, the rest replaced (same rule as `nox.core.config`)."""
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def load_existing_user_layer(path: Path) -> dict[str, Any]:
    """Parse `user.yaml` with the same loader the core uses.

    An absent, empty or non-mapping file is an empty layer, never an error; a file that does not
    parse at all raises `UserConfigError`, so a caller about to write cannot merge its patch into
    a silently empty document and drop everything the user had in there.
    """
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise UserConfigError(f"{path} is not valid YAML: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _round_trip_yaml() -> YAML:
    """The one round-trip parser/emitter configuration both halves of a write share."""
    editor = YAML(typ="rt")
    editor.preserve_quotes = True
    editor.allow_unicode = True
    editor.width = 4096  # never re-wrap a line the user wrote (or a long path/URL)
    return editor


def _load_round_trip(path: Path, editor: YAML) -> tuple[Any, str]:
    """`(document, header)` for a write.

    `document` is the existing mapping with its comments attached, or `None` when there is nothing
    to merge into (no file, an empty one, or a document that is not a mapping at all - the same
    cases `load_existing_user_layer` reports as an empty layer).

    `header` is the comment text to put in front of the document ruamel emits. A file that is
    *only* comments parses to nothing, so its lines would be lost on the dump; they are carried
    here instead, because a comment someone wrote is exactly what #22 is about.
    """
    if not path.is_file():
        return None, USER_CONFIG_HEADER
    text = path.read_text(encoding="utf-8")
    try:
        document = editor.load(text)
    except YAMLError as exc:
        raise UserConfigError(f"{path} is not valid YAML: {exc}") from exc
    if isinstance(document, MutableMapping):
        return document, ""
    comments_only = all(
        not line.strip() or line.lstrip().startswith("#") for line in text.splitlines()
    )
    return None, (text if comments_only and text.strip() else USER_CONFIG_HEADER)


def _merge_into(target: MutableMapping[str, Any], overlay: Mapping[str, Any]) -> None:
    """Deep-merge `overlay` *into* `target` in place - the round-trip twin of `deep_merge`.

    In place is the whole point: ruamel keeps a node's comments on the container it was loaded
    into, so assigning only the changed leaves leaves every comment, key order and quoting style
    of the rest of the document exactly as the user wrote it.
    """
    for key, value in overlay.items():
        current = target.get(key)
        if isinstance(value, Mapping) and isinstance(current, MutableMapping):
            _merge_into(current, value)
        else:
            target[key] = value


def write_user_config(path: Path, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge `patch` into whatever `user.yaml` already has and write it back.

    Comments, key order and quoting survive (#22); an absent or empty file is created with the
    header above. Never touches `config/defaults.yaml`. Returns the merged document written.

    Raises `UserConfigError` - leaving the file untouched - when it does not parse as YAML.
    """
    editor = _round_trip_yaml()
    document, header = _load_round_trip(path, editor)
    if document is None:
        # Nothing to merge into: the header (this module's, or the comment-only file's own text)
        # is written verbatim in front of a fresh block mapping. From the next write on it is the
        # document's leading comment and ruamel carries it along by itself.
        document = editor.map()
    _merge_into(document, patch)
    if header and not header.endswith("\n"):
        header += "\n"

    buffer = io.StringIO()
    editor.dump(document, buffer)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + buffer.getvalue(), encoding="utf-8")
    return cast(dict[str, Any], document)


def load_merged_config(profile_id: str | None = None) -> NoxConfig:
    """Load Defaults + User (+ profile) exactly as the core does, from the environment alone.

    For processes that do not get a `NoxConfig` handed to them - notably a plugin worker, which
    only receives its own manifest block - and for validating a candidate `user.yaml` before
    writing it.
    """
    user_path = resolve_user_config_path()
    user = user_path if user_path.is_file() else None
    return load_config(resolve_defaults_path(), user, profile_id)
