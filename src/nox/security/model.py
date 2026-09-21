"""Security contract: permission model, profiles, privacy state, kill switch, audit.

Permission = f(agent, tool, action, mode, risk, target) -> allow | confirm | deny
Enforced below the AI layer: every tool call passes through PermissionEngine.check() before
execution. Hard prohibitions (config security.hard_prohibitions) are never overridable by
profiles, runtime overrides, plugins, voice, chat or an LLM. Changing the security core requires
PIN + controlled restart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class Risk(StrEnum):
    READ = "read"  # observe only, no side effects
    LOW = "low"  # reversible local side effects (write to Nox data, UI)
    MEDIUM = "medium"  # reversible side effects outside Nox (files in allowed roots, OBS scenes)
    HIGH = "high"  # hard to reverse or external (push, publish, delete, money, accounts)
    CRITICAL = "critical"  # security core, secrets, game boundary; always deny or PIN


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"  # user confirms via GUI/voice-with-hotkey/Telegram-limited; timeout = deny
    DENY = "deny"


class AutonomyLevel(int):
    """How much Nox may do on its own: L0 answers only, L5 runs long tasks unattended."""


class PermissionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    agent: str  # companion|stream|coach|coding|project|research|system|memory|shell|remote
    tool: str  # tool id from the tool catalog, e.g. filesystem, obs, twitch
    action: str  # tool-specific verb, e.g. write, delete, scene.switch
    mode: str  # current primary mode
    risk: Risk
    target: str = ""  # path, scene name, repo, url ... used for zone/root matching
    task_id: str | None = None
    origin: str = "local"  # local | voice | telegram | chat | plugin


class PermissionResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    decision: Decision
    rule_id: str
    reason: str = ""
    requires_pin: bool = False
    grant_id: str | None = None  # set when a temporary grant ("mach einfach") applied


class ProfileRule(BaseModel):
    """One row of a profile. Matching is by glob on each field; first matching rule wins,
    but DENY from hard prohibitions is evaluated first and cannot be overridden."""

    id: str
    agent: str = "*"
    tool: str = "*"
    action: str = "*"
    mode: str = "*"
    risk: str = "*"
    target: str = "*"
    decision: Decision
    reason: str = ""


class Profile(BaseModel):
    id: str  # companion | coding | stream | research | work | offline
    description: str = ""
    rules: list[ProfileRule]
    # Restrictions a profile may impose beyond tool permissions (router, egress, memory, capture):
    cloud_allowed: bool = True
    egress_allowlist: list[str] = Field(default_factory=list)  # empty = inherit global allowlist
    # Additive to security.loopback_allowlist: extra loopback services this profile may reach while
    # privacy mode is private or offline - the recording software's control port, say.
    loopback_allowlist: list[str] = Field(default_factory=list)
    memory_writes_allowed: bool = True
    screenshots_to_cloud: bool = False
    filesystem_roots: list[str] = Field(default_factory=list)  # empty = inherit global roots
    tools_allowed: list[str] = Field(default_factory=list)  # empty = inherit
    integrations_allowed: list[str] = Field(default_factory=list)


class TemporaryGrant(BaseModel):
    """'Mach einfach': task- or project-scoped elevation with automatic expiry.

    Never lifts hard prohibitions.
    """

    grant_id: str
    scope: str  # task:<id> | project:<id> | repo:<path>
    max_risk: Risk
    expires_at: datetime
    granted_by: str = "user"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PermissionEngine(Protocol):
    def check(self, request: PermissionRequest) -> PermissionResult: ...
    def active_profile(self) -> Profile: ...
    def set_profile(self, profile_id: str, *, by: str) -> None: ...
    def grant_temporary(self, grant: TemporaryGrant) -> None: ...
    def revoke(self, grant_id: str) -> None: ...


class KillSwitch(Protocol):
    """Implemented below the AI layer. Triggerable by hotkey, tray, UI, Telegram (/stop), voice.

    Effects: system.level=safe_mode, stop all workers/plugins/tools, capture off, cloud off,
    audit entry. Recovery only by explicit manual restart.
    """

    async def trigger(self, *, by: str, reason: str = "") -> None: ...
    def is_engaged(self) -> bool: ...


class PanicMode(Protocol):
    """Immediately disable microphone, camera, screen capture and cloud/external communication."""

    async def engage(self, *, by: str) -> None: ...
    async def release(self, *, by: str, pin_ok: bool) -> None: ...


class AuditLog(Protocol):
    """Append-only, hash-chained. Every tool action including denied/aborted/failed ones."""

    def append(
        self,
        *,
        actor: str,
        tool: str,
        action: str,
        target: str,
        decision: str,
        result: str,
        task_id: str | None = None,
        details: dict[str, str] | None = None,
    ) -> int: ...
    def verify_chain(self) -> bool: ...


class SecretStore(Protocol):
    """Windows Credential Manager via keyring. Names are namespaced: nox/<component>/<key>."""

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...
