"""Loading the four configuration layers: defaults -> user -> profile -> runtime overrides.

Every layer is deep-merged onto the previous one (dicts recursively, lists and scalars replaced)
and the result is validated. A value in the user layer that fails validation is rejected on its
own - with a `ConfigWarning` naming its dotted path - and the rest of the user layer applies; a
protective setting that is rejected takes its strictest value rather than the default (see
`nox.core.config.user_layer`). A user layer that cannot be read at all is replaced by those strict
values. An invalid profile layer is discarded as a whole with a warning. Runtime overrides live only
in memory, and an invalid one raises `ConfigError`: that is a programming or dashboard input error
whose caller has to surface it.

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
from nox.core.config.user_layer import Rejection, fail_closed_overlay, prune_invalid

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

    def apply(self, layer: str, source: Path, data: dict[str, Any], *, warn: bool = True) -> bool:
        """Merge and validate one layer. Returns False, and records a warning, if it is invalid."""
        candidate = deep_merge(self.merged, data)
        try:
            model = NoxConfig.model_validate(candidate)
        except ValidationError as exc:
            if warn:
                self.warn(layer, str(source), format_validation_error(exc))
            return False
        self.merged = candidate
        self.model = model
        return True

    def apply_per_key(self, layer: str, source: Path, data: dict[str, Any]) -> None:
        """Merge one layer, rejecting only the values that fail validation.

        Each rejected value gets its own warning. A rejected protective setting takes its strictest
        value; a layer that cannot be pruned into a valid one is lost as a whole, and then every
        protective setting does.
        """
        if self.apply(layer, source, data, warn=False):
            return
        pruned, rejections = prune_invalid(self.merged, data, NoxConfig.model_validate)
        if pruned is None:
            detail = "; ".join(f"{r.dotted}: {r.message}" for r in rejections)
            self.lose(layer, str(source), detail or "the layer does not validate as a whole")
            return
        overlay, strict = fail_closed_overlay(self.merged, data, rejections)
        for rejection in rejections:
            self.warn(
                layer, str(source), _rejection_message(rejection, strict), key=rejection.dotted
            )
        if not self.apply(layer, source, deep_merge(pruned, overlay)):
            self.lose(layer, str(source), "the remaining values do not validate")

    def lose(self, layer: str, source: str, reason: str) -> None:
        """A layer that could not be used at all: protective settings take their strictest value."""
        overlay, strict = fail_closed_overlay(self.merged, {}, None)
        self.warn(
            layer,
            source,
            f"{reason}; protective settings use their strictest value ({', '.join(strict)})",
        )
        candidate = deep_merge(self.merged, overlay)
        self.model = NoxConfig.model_validate(candidate)
        self.merged = candidate

    def warn(self, layer: str, source: str, message: str, *, key: str = "") -> None:
        self.warnings.append(ConfigWarning(layer=layer, source=source, message=message, key=key))


def _rejection_message(rejection: Rejection, strict: list[str]) -> str:
    """`<dotted.path>: <why> - rejected, <what applies instead>`."""
    dotted = rejection.dotted
    covered = [key for key in strict if key == dotted or key.startswith(dotted + ".")]
    fallback = (
        f"strictest value used for {', '.join(covered)}" if covered else "the default applies"
    )
    return f"{dotted}: {rejection.message} - rejected, {fallback}"


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
                user_layer = read_yaml_layer(user_path)
            except ConfigError as exc:
                layers.lose("user", str(user_path), str(exc))
            else:
                layers.apply_per_key("user", user_path, user_layer)
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
