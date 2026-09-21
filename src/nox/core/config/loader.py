"""Loading the four configuration layers: defaults -> user -> profile -> runtime overrides.

Every layer is deep-merged onto the previous one (dicts recursively, lists and scalars replaced)
and the result is validated once. An invalid user or profile layer is discarded with a
`ConfigWarning` and the previous layers keep applying, so a typo in a hand-edited file degrades to
"the defaults, plus a visible warning" instead of a core that will not start. Runtime overrides
live only in memory, and an invalid one raises `ConfigError`: that is a programming or dashboard
input error whose caller has to surface it.

`security.hard_prohibitions` may only ever be extended, never shortened. That check lives in
`SecurityConfig`, so every layer passes through it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from nox.core.config.model import NoxConfig
from nox.core.config.types import ConfigError, ConfigWarning

__all__ = [
    "deep_merge",
    "format_validation_error",
    "load_config",
    "profile_path",
    "read_yaml_layer",
]


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict: nested dicts merged recursively, everything else replaced by `overlay`."""
    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = value
    return result


def format_validation_error(exc: ValidationError) -> str:
    """One line per error with its dotted config path, e.g. `ipc.port: Input should be ...`."""
    lines = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"{location}: {err['msg']}")
    return "; ".join(lines)


def read_yaml_layer(path: Path) -> dict[str, Any]:
    """Parse one YAML layer.

    Raises `ConfigError` for a file that cannot be read, does not parse, or is not a mapping.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def profile_path(profile_id: str, defaults_path: Path, user_path: Path | None) -> Path | None:
    """Locate `<id>.yaml`. A user profile, next to `user.yaml`, shadows the shipped one."""
    candidates: list[Path] = []
    if user_path is not None:
        candidates.append(user_path.parent / "profiles" / f"{profile_id}.yaml")
    candidates.append(defaults_path.parent / "profiles" / f"{profile_id}.yaml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class _Layers:
    """The merged mapping, the model it last validated to, and the warnings collected so far.

    Keeping the validated model means each layer is validated exactly once. The previous
    implementation threw every intermediate model away and validated the final mapping a fifth
    time.
    """

    def __init__(self, merged: dict[str, Any], model: NoxConfig) -> None:
        self.merged = merged
        self.model = model
        self.warnings: list[ConfigWarning] = []

    def apply(self, layer: str, source: Path, data: dict[str, Any]) -> bool:
        """Merge and validate one layer. Returns False, and records a warning, if it is invalid."""
        candidate = deep_merge(self.merged, data)
        try:
            model = NoxConfig.model_validate(candidate)
        except ValidationError as exc:
            self.warn(layer, str(source), format_validation_error(exc))
            return False
        self.merged = candidate
        self.model = model
        return True

    def warn(self, layer: str, source: str, message: str) -> None:
        self.warnings.append(ConfigWarning(layer=layer, source=source, message=message))


def load_config(
    defaults_path: Path,
    user_path: Path | None = None,
    profile_id: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> NoxConfig:
    """Load and merge the four layers; see the module docstring for the fallback rules.

    Warnings about rejected layers are available on the returned config as `.warnings`.
    """
    try:
        defaults = read_yaml_layer(defaults_path)
        layers = _Layers(defaults, NoxConfig.model_validate(defaults))
    except ConfigError:
        raise
    except ValidationError as exc:
        raise ConfigError(
            f"defaults invalid ({defaults_path}): {format_validation_error(exc)}"
        ) from exc

    if user_path is not None:
        if user_path.is_file():
            try:
                layers.apply("user", user_path, read_yaml_layer(user_path))
            except ConfigError as exc:
                layers.warn("user", str(user_path), str(exc))
        else:
            layers.warn("user", str(user_path), "file not found, using defaults")

    applied_profile: str | None = None
    if profile_id:
        path = profile_path(profile_id, defaults_path, user_path)
        if path is None:
            layers.warn("profile", profile_id, "profile file not found")
        else:
            try:
                if layers.apply("profile", path, read_yaml_layer(path)):
                    applied_profile = profile_id
            except ConfigError as exc:
                layers.warn("profile", str(path), str(exc))

    if overrides:
        candidate = deep_merge(layers.merged, dict(overrides))
        try:
            layers.model = NoxConfig.model_validate(candidate)
        except ValidationError as exc:
            raise ConfigError(f"runtime override invalid: {format_validation_error(exc)}") from exc
        layers.merged = candidate

    return NoxConfig.loaded(layers.model, warnings=layers.warnings, profile_id=applied_profile)
