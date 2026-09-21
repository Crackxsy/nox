"""The building blocks every configuration section is made of: the strict base model, the two
error types, and the annotated path type that expands environment references.

Path values may be written as `${VAR}`, `$VAR`, `%VAR%` or `~`. Expansion happens once, during
validation, so every consumer sees a real filesystem path and no part of the code has to remember
to expand anything. A reference the environment cannot satisfy is an error naming the variable,
never a directory literally called `${APPDATA}`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict

__all__ = [
    "ConfigError",
    "ConfigWarning",
    "ExpandedPath",
    "OptionalExpandedPath",
    "StrictSection",
    "expand_path",
    "require_ordered",
]


class ConfigError(Exception):
    """The defaults layer or a runtime override is invalid.

    User and profile layers never raise: they are rejected with a `ConfigWarning` and the previous
    layers keep applying.
    """


class ConfigWarning(BaseModel):
    """A non-fatal problem with one configuration layer.

    `layer` is one of `defaults`, `user`, `profile`, `override`.
    """

    model_config = ConfigDict(frozen=True)
    layer: str
    source: str
    message: str

    def __str__(self) -> str:
        return f"[{self.layer}] {self.source}: {self.message}"


class StrictSection(BaseModel):
    """Base for every configuration section.

    Unknown keys are errors, so a typo is reported with its dotted path instead of being ignored
    until someone wonders why a setting has no effect. Defaults are validated too, so a default
    that violates its own bound fails the test suite rather than a user's first start.
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)


# ---- path expansion -----------------------------------------------------------------------------

_VAR = re.compile(r"\$\{(\w+)\}|\$(\w+)|%(\w+)%")

#: Windows locations that a non-Windows run - a test box, a contributor's machine - has no
#: environment variable for. Substituting them keeps the shipped defaults loadable everywhere. Any
#: other unresolved variable is an error, because guessing would put user data somewhere nobody
#: asked for.
_FALLBACKS: dict[str, Path] = {
    "APPDATA": Path.home() / "AppData" / "Roaming",
    "LOCALAPPDATA": Path.home() / "AppData" / "Local",
    "USERPROFILE": Path.home(),
    "HOME": Path.home(),
}


def expand_path(value: str) -> Path:
    """Expand `${VAR}`, `$VAR`, `%VAR%` and `~` in `value` and return it as a `Path`.

    Raises `ValueError` naming every variable that is neither in the environment nor one of the
    known Windows locations above.
    """
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2) or match.group(3)
        from_environment = os.environ.get(name)
        if from_environment:
            return from_environment
        fallback = _FALLBACKS.get(name.upper())
        if fallback is not None:
            return str(fallback)
        unresolved.append(name)
        return match.group(0)

    expanded = _VAR.sub(replace, value)
    if unresolved:
        raise ValueError(
            "unresolved environment reference(s) in path: " + ", ".join(sorted(set(unresolved)))
        )
    return Path(os.path.expanduser(expanded))


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("path must not be empty")
        return expand_path(value)
    if isinstance(value, Path):
        return expand_path(str(value))
    return value


def _expand_optional(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return _expand(value)


#: A path setting that must be present. `""` is rejected: an empty path is never what was meant.
ExpandedPath = Annotated[Path, BeforeValidator(_expand)]

#: A path setting that may be left unset; `null` and `""` both mean "not set".
OptionalExpandedPath = Annotated[Path | None, BeforeValidator(_expand_optional)]


# ---- cross-field bounds -------------------------------------------------------------------------


def require_ordered[ModelT: BaseModel](model: ModelT, low: str, high: str) -> ModelT:
    """Assert `model.<low> <= model.<high>` for a pair of numeric settings.

    Used from an after-validator. A reversed pair - an idle threshold above the away threshold, a
    minimum capture rate above the maximum - does not fail loudly on its own; it just makes the
    feature never fire, so it is rejected while the file is being read.
    """
    low_value = getattr(model, low)
    high_value = getattr(model, high)
    if low_value is not None and high_value is not None and high_value < low_value:
        raise ValueError(f"{high} must not be smaller than {low}")
    return model
