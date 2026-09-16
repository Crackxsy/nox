"""What the dashboard may edit, and what each of those settings *is* - derived, not hand-kept.

Two hand-written things live here, because neither can be read off a pydantic model:

* :data:`EDITABLE_PATHS` - the allow-list. `config.set` writes nothing that is not in it, so a
  compromised or buggy dashboard cannot rewrite `security.hard_prohibitions`, a filesystem root or
  a supervisor command line through the Settings page.
* :data:`LIVE_APPLY_PATHS` - the paths the running core can genuinely adopt without a restart.
  Everything else is honestly reported as `restart_required`; claiming a live apply that does not
  happen would be exactly the "no fake capabilities" failure the project forbids.

Everything else - the value type, the enum options, the numeric bounds - is read out of
`nox.core.config.NoxConfig` at call time, so a field that gains an option or a tighter bound never
needs a second edit here.
"""

from __future__ import annotations

from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from annotated_types import Ge, Gt, Le, Lt
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from nox.core.config import NoxConfig

#: Dotted path -> UI group. The order is the order the dashboard renders.
EDITABLE_PATHS: dict[str, str] = {
    "identity.name": "identity",
    "identity.user_display_name": "identity",
    "identity.ui_language": "identity",
    "identity.speech_language": "identity",
    "voice.stt.model": "voice",
    "voice.stt.language": "voice",
    "voice.stt.wake_word": "voice",
    "voice.stt.push_to_talk_hotkey": "voice",
    "voice.tts.voice": "voice",
    "voice.tts.rate": "voice",
    "voice.tts.volume": "voice",
    "voice.channels.routing": "voice",
    "privacy.capture.microphone": "privacy",
    "privacy.capture.camera": "privacy",
    "privacy.capture.screen": "privacy",
    "security.profile": "privacy",
    "ai.router.default_reasoner": "ai",
    "ai.router.fallback_chain": "ai",
    "pet.variant": "pet",
    "pet.greeting_enabled": "pet",
    "stream.twitch.channel": "integrations",
    "remote.enabled": "remote",
    "memory.full_scan_on_boot": "memory",
    "plugins.enabled": "plugins",
}

#: Paths the running core adopts immediately (see `nox.settings.editor` for what each one does).
#: Everything not listed here is written to `user.yaml` and reported as `restart_required`.
LIVE_APPLY_PATHS: frozenset[str] = frozenset(
    {
        "identity.name",
        "identity.user_display_name",
        "identity.ui_language",
        "privacy.capture.microphone",
        "privacy.capture.camera",
        "privacy.capture.screen",
        "pet.variant",
    }
)

#: The value kinds the dashboard knows how to render. Anything a model expresses that does not map
#: onto one of these is simply not offered for editing (it never reaches `EDITABLE_PATHS`).
ValueKind = Literal["string", "int", "float", "bool", "enum", "list[str]"]


class UnknownSettingError(KeyError):
    """The dotted path is not an editable setting (unknown, or not on the allow-list)."""


class ConfigFieldSpec(BaseModel):
    """One editable setting, as the dashboard needs it to render a control."""

    path: str
    type: ValueKind
    options: list[str] | None = None
    min: float | None = None
    max: float | None = None
    restart_required: bool
    group: str


def _resolve_field(path: str) -> FieldInfo:
    """Walk `NoxConfig`'s nested models along a dotted path and return the leaf's `FieldInfo`."""
    model: type[BaseModel] = NoxConfig
    parts = path.split(".")
    for index, part in enumerate(parts):
        field = model.model_fields.get(part)
        if field is None:
            raise UnknownSettingError(path)
        if index == len(parts) - 1:
            return field
        annotation = field.annotation
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            raise UnknownSettingError(path)
        model = annotation
    raise UnknownSettingError(path)


def _unwrap_optional(annotation: Any) -> Any:
    """`X | None` -> `X`; anything else is returned unchanged."""
    if get_origin(annotation) in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _kind_and_options(annotation: Any) -> tuple[ValueKind, list[str] | None]:
    annotation = _unwrap_optional(annotation)
    if get_origin(annotation) is Literal:
        return "enum", [str(a) for a in get_args(annotation)]
    if get_origin(annotation) is list:
        args = get_args(annotation)
        if args and args[0] is str:
            return "list[str]", None
        raise UnknownSettingError("unsupported list element type")
    if annotation is bool:
        return "bool", None
    if annotation is int:
        return "int", None
    if annotation is float:
        return "float", None
    if annotation is str:
        return "string", None
    raise UnknownSettingError(f"unsupported annotation {annotation!r}")


def _bounds(field: FieldInfo) -> tuple[float | None, float | None]:
    """`ge`/`gt` and `le`/`lt` constraints as plain numbers (the UI only needs a slider range)."""
    low: float | None = None
    high: float | None = None
    for meta in field.metadata:
        # `annotated_types` types the bound as a comparable protocol, not a number; every bound
        # this configuration actually uses is an int or a float, and a non-numeric one is simply
        # not reported rather than guessed at.
        if isinstance(meta, Ge | Gt):
            low = _as_float(meta.ge if isinstance(meta, Ge) else meta.gt)
        elif isinstance(meta, Le | Lt):
            high = _as_float(meta.le if isinstance(meta, Le) else meta.lt)
    return low, high


def _as_float(bound: Any) -> float | None:
    return float(bound) if isinstance(bound, int | float) else None


def describe(path: str) -> ConfigFieldSpec:
    """The schema entry for one editable path. Raises `UnknownSettingError` for anything else."""
    group = EDITABLE_PATHS.get(path)
    if group is None:
        raise UnknownSettingError(path)
    field = _resolve_field(path)
    kind, options = _kind_and_options(field.annotation)
    low, high = _bounds(field)
    return ConfigFieldSpec(
        path=path,
        type=kind,
        options=options,
        min=low,
        max=high,
        restart_required=path not in LIVE_APPLY_PATHS,
        group=group,
    )


def describe_all() -> list[ConfigFieldSpec]:
    """Every editable setting, in `EDITABLE_PATHS` order."""
    return [describe(path) for path in EDITABLE_PATHS]


def read_value(config: NoxConfig, path: str) -> Any:
    """The current value of `path` on a loaded config, JSON-ready (paths become strings)."""
    node: Any = config
    for part in path.split("."):
        node = getattr(node, part)
    if isinstance(node, BaseModel):  # pragma: no cover - not reachable for editable leaves
        return node.model_dump(mode="json")
    return node


def nest(path: str, value: Any) -> dict[str, Any]:
    """`("a.b.c", 1)` -> `{"a": {"b": {"c": 1}}}`, the shape a config layer is written in."""
    parts = path.split(".")
    patch: dict[str, Any] = {}
    node = patch
    for part in parts[:-1]:
        child: dict[str, Any] = {}
        node[part] = child
        node = child
    node[parts[-1]] = value
    return patch
