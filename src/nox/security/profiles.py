"""Permission profiles: load `Profile` models from `config/profiles/<id>.yaml` (Security Model §3,
ADR-010).

File layout: a profile file carries the permission profile under the top-level key `permissions`
(so the same file can also hold config overlays merged by `nox.core.config`). A file whose top level
is the `Profile` document itself (Security Model §3 literal layout) is accepted as well.
A profile that would allow a hard prohibition fails to load (§4: config may only add prohibitions).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import ValidationError

from nox.security._logging import get_logger
from nox.security.model import Decision, Profile, ProfileRule
from nox.security.prohibitions import HardProhibitionRemovedError, matching_hard_prohibition

log = get_logger(__name__)

PROFILE_IDS: tuple[str, ...] = (
    "companion",
    "coding",
    "stream",
    "research",
    "work",
    "offline",
    "rocket_league",
)
PERMISSIONS_KEY = "permissions"


class ProfileError(ValueError):
    """A profile file is missing, malformed or violates the security model."""


class ProfileProvider(Protocol):
    def get(self, profile_id: str) -> Profile: ...
    def available(self) -> list[str]: ...


def _rule_targets_prohibition(rule: ProfileRule) -> str | None:
    for candidate in (rule.tool, rule.action, f"{rule.tool}.{rule.action}"):
        if candidate in ("*", "*.*"):
            continue
        hit = matching_hard_prohibition(candidate)
        if hit is not None:
            return hit
    return None


def validate_profile(profile: Profile) -> Profile:
    """Reject profiles that try to allow/confirm a hard prohibition or carry duplicate rule ids."""
    seen: set[str] = set()
    for rule in profile.rules:
        if rule.id in seen:
            raise ProfileError(f"profile {profile.id!r}: duplicate rule id {rule.id!r}")
        seen.add(rule.id)
        if rule.decision is Decision.DENY:
            continue
        hit = _rule_targets_prohibition(rule)
        if hit is not None:
            raise HardProhibitionRemovedError(
                f"profile {profile.id!r}: rule {rule.id!r} would {rule.decision.value} "
                f"hard prohibition {hit!r}"
            )
    return profile


def parse_profile(data: Mapping[str, Any], *, expected_id: str | None = None) -> Profile:
    """Build a validated Profile from a parsed YAML mapping (either layout, see module doc)."""
    block: Any = data.get(PERMISSIONS_KEY, data)
    if not isinstance(block, Mapping):
        raise ProfileError(f"{PERMISSIONS_KEY!r} must be a mapping")
    payload = dict(block)
    if expected_id and "id" not in payload:
        payload["id"] = expected_id
    try:
        profile = Profile.model_validate(payload)
    except ValidationError as exc:
        raise ProfileError(f"invalid profile: {exc}") from exc
    if expected_id and profile.id != expected_id:
        raise ProfileError(f"profile id {profile.id!r} does not match file name {expected_id!r}")
    return validate_profile(profile)


def load_profile(path: Path, *, expected_id: str | None = None) -> Profile:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"cannot read profile {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ProfileError(f"{path}: top level must be a mapping")
    return parse_profile(data, expected_id=expected_id or path.stem)


class YamlProfileProvider:
    """Loads `<directory>/<id>.yaml` on demand and caches validated profiles."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)
        self._cache: dict[str, Profile] = {}

    @property
    def directory(self) -> Path:
        return self._directory

    def available(self) -> list[str]:
        if not self._directory.is_dir():
            return []
        return sorted(p.stem for p in self._directory.glob("*.yaml"))

    def get(self, profile_id: str) -> Profile:
        cached = self._cache.get(profile_id)
        if cached is not None:
            return cached
        path = self._directory / f"{profile_id}.yaml"
        if not path.is_file():
            raise KeyError(profile_id)
        profile = load_profile(path, expected_id=profile_id)
        self._cache[profile_id] = profile
        log.debug("security.profile_loaded", profile=profile_id, rules=len(profile.rules))
        return profile

    def reload(self) -> None:
        self._cache.clear()


class InMemoryProfileProvider:
    """Profiles supplied in code (tests, embedded defaults); validated against the hard list."""

    def __init__(self, profiles: Iterable[Profile]) -> None:
        self._profiles = {p.id: validate_profile(p) for p in profiles}

    def available(self) -> list[str]:
        return sorted(self._profiles)

    def get(self, profile_id: str) -> Profile:
        try:
            return self._profiles[profile_id]
        except KeyError:
            raise KeyError(profile_id) from None
