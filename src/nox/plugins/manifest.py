"""Plugin manifest schema, loader and core-side authorization (Plugin Architecture,/013).

A plugin is a package plus `plugins/<id>/manifest.yaml`. Nothing is spawned before the manifest
validates: tools must live in the `<id>.` namespace and may never name a hard prohibition
(`nox.security.prohibitions`), secrets must be `nox/<id>/...` Credential-Manager names, and every
`network.egress` entry must be authorized against the *active profile's* allow-lists by the core
(the worker then scopes its own `EgressGuard` to exactly this list).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nox.security.egress import NO_EGRESS, entry_matches, is_loopback, loopback_entry_matches
from nox.security.model import Profile, Risk
from nox.security.prohibitions import is_hard_prohibited
from nox.security.secrets import SECRET_NAME_RE

#: Manifest `api_version` values this runtime understands.
SUPPORTED_API_VERSIONS: frozenset[int] = frozenset({1})
API_VERSION = 1
MANIFEST_FILE = "manifest.yaml"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
#: Dotted lowercase names (tools, events): at least two segments, e.g. `echo.ping`.
_DOTTED_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_*]+)+$")
#: `package.module:callable`
_ENTRY_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)


class ManifestError(ValueError):
    """A manifest is missing, unparsable or violates a plugin boundary rule."""


def split_endpoint(entry: str) -> tuple[str, int]:
    """`host:port` -> (host, port). Manifests must be explicit; wildcards/ports are not allowed."""
    text = entry.strip().lower()
    if not text or text == NO_EGRESS:
        raise ManifestError(f"invalid network.egress entry {entry!r}")
    if "://" in text:
        text = text.split("://", 1)[1].split("/", 1)[0]
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ManifestError(f"network.egress entry must be host:port, got {entry!r}")
    number = int(port)
    if not 1 <= number <= 65535:
        raise ManifestError(f"network.egress entry has an invalid port: {entry!r}")
    return host.strip("[]"), number


# ---- schema -------------------------------------------------------------------------------------


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PluginPermission(_Section):
    """One requested capability. `risk` is authoritative: the worker may not register it lower."""

    tool: str
    #: Conservative default: an undeclared risk is never treated as `read`.
    risk: Risk = Risk.MEDIUM


class PluginEvents(_Section):
    emits: list[str] = Field(default_factory=list)
    listens: list[str] = Field(default_factory=list)


class PluginNetwork(_Section):
    #: `host:port` allow-list; the worker's `EgressGuard` is scoped to exactly these.
    egress: list[str] = Field(default_factory=list)


class PluginResources(_Section):
    memory_mb: int = Field(default=256, ge=16, le=8192)
    priority: str = Field(default="normal", pattern=r"^(low|normal|high)$")


class PluginManifest(BaseModel):
    """`plugins/<id>/manifest.yaml` (Plugin Architecture "Plugin = manifest + worker")."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    version: str = "0.0.0"
    api_version: int = API_VERSION
    entry: str  # `package.module:callable`, called as `create(plugin_api)`
    profiles: list[str] = Field(default_factory=list)  # empty = every profile
    permissions: list[PluginPermission] = Field(default_factory=list)
    events: PluginEvents = Field(default_factory=PluginEvents)
    secrets: list[str] = Field(default_factory=list)
    network: PluginNetwork = Field(default_factory=PluginNetwork)
    resources: PluginResources = Field(default_factory=PluginResources)
    #: Static configuration block handed to the plugin as `PluginApi.config` (never secrets).
    config: dict[str, Any] = Field(default_factory=dict)

    # -- derived ---------------------------------------------------------------------------------

    @property
    def namespace(self) -> str:
        return f"{self.id}."

    @property
    def secret_prefix(self) -> str:
        return f"nox/{self.id}/"

    def declares_tool(self, name: str) -> PluginPermission | None:
        for permission in self.permissions:
            if permission.tool == name:
                return permission
        return None

    def event_namespaces(self) -> frozenset[str]:
        """Service namespaces the hub must grant this plugin: its id plus every emitted prefix.

        The hub's inbound-event gate works on namespaces (`<ns>.**`); the exact `emits` list is
        enforced by `PluginApi.events.emit` on top of it. Only namespaces the manifest itself
        declares are granted, so a plugin can never publish outside what was validated here.
        """
        return frozenset({self.id, *(name.split(".", 1)[0] for name in self.events.emits)})

    def matches_profile(self, profile_id: str) -> bool:
        return not self.profiles or profile_id in self.profiles

    # -- validation ------------------------------------------------------------------------------

    @model_validator(mode="after")
    def _validate_boundaries(self) -> PluginManifest:
        if not _ID_RE.match(self.id):
            raise ValueError(f"plugin id {self.id!r} must be lowercase [a-z][a-z0-9_]{{1,31}}")
        if self.api_version not in SUPPORTED_API_VERSIONS:
            raise ValueError(
                f"unsupported api_version {self.api_version} "
                f"(supported: {sorted(SUPPORTED_API_VERSIONS)})"
            )
        if not _ENTRY_RE.match(self.entry):
            raise ValueError(f"entry must be 'package.module:callable', got {self.entry!r}")
        for permission in self.permissions:
            tool = permission.tool
            if is_hard_prohibited(tool):
                raise ValueError(f"tool {tool!r} is hard-prohibited and can never be granted")
            if not tool.startswith(self.namespace):
                raise ValueError(
                    f"tool {tool!r} is outside the plugin's {self.namespace!r} namespace"
                )
            if not _DOTTED_RE.match(tool):
                raise ValueError(f"tool name {tool!r} must be dotted lowercase")
        for name in (*self.events.emits, *self.events.listens):
            if not _DOTTED_RE.match(name):
                raise ValueError(f"event name {name!r} must be dotted lowercase")
        for secret in self.secrets:
            if not SECRET_NAME_RE.match(secret):
                raise ValueError(f"secret name {secret!r} must look like nox/<component>/<key>")
            if not secret.startswith(self.secret_prefix):
                raise ValueError(
                    f"secret {secret!r} is outside the plugin's {self.secret_prefix!r} namespace"
                )
        for entry in self.network.egress:
            split_endpoint(entry)
        return self


# ---- loading ------------------------------------------------------------------------------------


def parse_manifest(
    data: Mapping[str, Any], *, expected_id: str | None = None, source: str = "<memory>"
) -> PluginManifest:
    """Validate a manifest mapping; `expected_id` pins it to its directory name."""
    try:
        manifest = PluginManifest.model_validate(dict(data))
    except ValueError as exc:
        raise ManifestError(f"{source}: {_first_error(exc)}") from exc
    if expected_id is not None and manifest.id != expected_id:
        raise ManifestError(
            f"{source}: manifest id {manifest.id!r} does not match its directory {expected_id!r}"
        )
    return manifest


def load_manifest(path: Path, *, expected_id: str | None = None) -> PluginManifest:
    """Load `plugins/<id>/manifest.yaml` (or the plugin directory containing it)."""
    file = path / MANIFEST_FILE if path.is_dir() else path
    if expected_id is None:
        expected_id = file.parent.name
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read {file}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in {file}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError(f"{file}: top level must be a mapping")
    return parse_manifest(raw, expected_id=expected_id, source=str(file))


def discover_manifest_paths(plugins_dir: Path) -> list[Path]:
    """Every `<plugins_dir>/<id>/manifest.yaml`, sorted by id. Missing directory -> empty list."""
    if not plugins_dir.is_dir():
        return []
    return sorted(
        (d / MANIFEST_FILE for d in plugins_dir.iterdir() if (d / MANIFEST_FILE).is_file()),
        key=lambda p: p.parent.name,
    )


def _first_error(exc: ValueError) -> str:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    parts = []
    for err in errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "<root>"
        parts.append(f"{loc}: {err.get('msg', '')}")
    return "; ".join(parts) or str(exc)


# ---- core-side egress authorization ----------------------------------------------------


class EgressAuthorization(BaseModel):
    """Per-entry result of authorizing a manifest's egress against the active profile."""

    model_config = ConfigDict(frozen=True)
    entry: str
    allowed: bool
    rule_id: str
    reason: str = ""


def authorize_egress(
    manifest: PluginManifest,
    profile: Profile,
    *,
    global_allowlist: Sequence[str] = (),
    loopback_allowlist: Sequence[str] = (),
) -> list[EgressAuthorization]:
    """Decide every `network.egress` entry the way `EgressGuard` would in FULL/BALANCED.

    Loopback endpoints must be on the merged loopback allow-list, everything else on the
    profile's `egress_allowlist` (or the global list when the profile inherits it).
    """
    results: list[EgressAuthorization] = []
    merged_loopback = tuple(loopback_allowlist) + tuple(profile.loopback_allowlist)
    for entry in manifest.network.egress:
        host, port = split_endpoint(entry)
        if is_loopback(host):
            results.append(
                _decide(entry, merged_loopback, host, port, f"profile.{profile.id}.loopback", True)
            )
            continue
        if profile.egress_allowlist or not profile.cloud_allowed:
            allowlist: Sequence[str] = profile.egress_allowlist
            rule = f"profile.{profile.id}.egress_allowlist"
        else:
            allowlist = global_allowlist
            rule = "global.egress_allowlist"
        results.append(_decide(entry, allowlist, host, port, rule, False))
    return results


def _decide(
    entry: str,
    allowlist: Sequence[str],
    host: str,
    port: int,
    rule_id: str,
    loopback: bool,
) -> EgressAuthorization:
    if any(item.strip().lower() == NO_EGRESS for item in allowlist):
        return EgressAuthorization(
            entry=entry, allowed=False, rule_id=f"{rule_id}.none", reason="profile forbids egress"
        )
    match = loopback_entry_matches if loopback else entry_matches
    for item in allowlist:
        if match(item, host, port):
            return EgressAuthorization(entry=entry, allowed=True, rule_id=rule_id, reason=item)
    return EgressAuthorization(
        entry=entry,
        allowed=False,
        rule_id=f"{rule_id}.default_deny",
        reason="not on the active profile's allow-list",
    )


def check_egress(
    manifest: PluginManifest,
    profile: Profile,
    *,
    global_allowlist: Sequence[str] = (),
    loopback_allowlist: Sequence[str] = (),
) -> list[EgressAuthorization]:
    """`authorize_egress` but raises `ManifestError` on the first denial (used before spawning)."""
    results = authorize_egress(
        manifest,
        profile,
        global_allowlist=global_allowlist,
        loopback_allowlist=loopback_allowlist,
    )
    denied = [r for r in results if not r.allowed]
    if denied:
        first = denied[0]
        raise ManifestError(
            f"plugin {manifest.id!r} declares egress {first.entry!r} which the active profile "
            f"{profile.id!r} does not allow ({first.rule_id}: {first.reason})"
        )
    return results


def declared_tools(manifest: PluginManifest) -> list[str]:
    return [p.tool for p in manifest.permissions]


def validate_tool_name(manifest: PluginManifest, name: str) -> PluginPermission:
    """Namespace + hard-prohibition + manifest-declaration check for a tool registration."""
    if is_hard_prohibited(name):
        raise ManifestError(f"tool {name!r} is hard-prohibited")
    if not name.startswith(manifest.namespace):
        raise ManifestError(f"tool {name!r} is outside the {manifest.namespace!r} namespace")
    permission = manifest.declares_tool(name)
    if permission is None:
        raise ManifestError(f"tool {name!r} is not declared in the manifest's permissions")
    return permission


def validate_secret_name(manifest: PluginManifest, name: str) -> str:
    """Only names listed in the manifest resolve; everything else is refused before any keyring
    access."""
    if name not in manifest.secrets:
        raise ManifestError(f"secret {name!r} is not declared by plugin {manifest.id!r}")
    return name


def iter_plugin_ids(plugins_dir: Path) -> Iterable[str]:
    return (p.parent.name for p in discover_manifest_paths(plugins_dir))
