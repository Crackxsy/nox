"""Default permission engine (Security Model §2, ADR-007, Tool Model pipeline step 4).

Evaluation order in `check()`: hard prohibitions -> safe mode -> privacy constraints -> critical
risk
(PIN only) -> temporary grants -> profile restrictions and rules (first match wins) -> default by
risk.
`check()` is pure given (request, profile, privacy snapshot, grants, clock) and audits every
decision.
Confirmations: `request_confirmation()` returns a grant_id, `reply()` resolves it,
`await_confirmation()`
waits (timeout = deny); `remember=True` creates a session-scoped `TemporaryGrant` for tool/action.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from nox.core.events import E, EventBus, PermissionDecided, PermissionRequested
from nox.core.state import PrivacyMode
from nox.security._events import publish, publish_nowait
from nox.security._logging import get_logger
from nox.security.model import (
    AuditLog,
    Decision,
    PermissionRequest,
    PermissionResult,
    Profile,
    ProfileRule,
    Risk,
    TemporaryGrant,
)
from nox.security.profiles import ProfileProvider
from nox.security.prohibitions import hard_prohibition_for

log = get_logger(__name__)

Clock = Callable[[], datetime]

RISK_ORDER: dict[Risk, int] = {
    Risk.READ: 0,
    Risk.LOW: 1,
    Risk.MEDIUM: 2,
    Risk.HIGH: 3,
    Risk.CRITICAL: 4,
}
DEFAULT_BY_RISK: dict[Risk, Decision] = {
    Risk.READ: Decision.ALLOW,
    Risk.LOW: Decision.ALLOW,
    Risk.MEDIUM: Decision.CONFIRM,
    Risk.HIGH: Decision.CONFIRM,
    Risk.CRITICAL: Decision.DENY,
}

# Tool-id classes used by privacy and profile constraints. Tool ids arrive as "filesystem" or
# "fs.read" style names, so every class lists both the bare id and the dotted prefix.
CLOUD_TOOL_PATTERNS: tuple[str, ...] = (
    "cloud",
    "cloud.*",
    "*.cloud",
    "*.cloud.*",
    "web",
    "web.*",
    "browser",
    "browser.*",
    "network",
    "network.*",
    "net",
    "net.*",
    "http",
    "http.*",
    "twitch",
    "twitch.*",
    "telegram",
    "telegram.*",
    "ai.cloud*",
)
CLOUD_ACTION_PATTERNS: tuple[str, ...] = ("*cloud*", "upload*", "publish*", "send_external*")
CAPTURE_TOOL_PATTERNS: tuple[str, ...] = (
    "capture",
    "capture.*",
    "screenshot",
    "screenshot.*",
    "screen",
    "screen.*",
    "camera",
    "camera.*",
    "microphone",
    "microphone.*",
    "clipboard",
    "clipboard.*",
    "vision",
    "vision.*",
)
SCREENSHOT_TOOL_PATTERNS: tuple[str, ...] = (
    "screenshot",
    "screenshot.*",
    "screen",
    "screen.*",
    "capture.screen*",
    "vision",
    "vision.*",
)
MEMORY_TOOL_PATTERNS: tuple[str, ...] = ("memory", "memory.*", "vault", "vault.*")
FILESYSTEM_TOOL_PATTERNS: tuple[str, ...] = (
    "filesystem",
    "filesystem.*",
    "fs",
    "fs.*",
    "file",
    "file.*",
)
READ_ACTIONS: frozenset[str] = frozenset(
    {"read", "search", "get", "list", "query", "stat", "exists", "now", "recall"}
)

_DRIVE = re.compile(r"^[a-zA-Z]:[/\\]")


# ---- injected state -----------------------------------------------------------------------------


class PrivacySnapshot(BaseModel):
    """What the engine needs to know about privacy and system level at decision time."""

    model_config = ConfigDict(frozen=True)
    mode: PrivacyMode = PrivacyMode.BALANCED
    zone_active: bool = False
    safe_mode: bool = False
    panic: bool = False


class PrivacyStateProvider(Protocol):
    def snapshot(self) -> PrivacySnapshot: ...


class GrantStore(Protocol):
    def add(self, grant: TemporaryGrant) -> None: ...
    def remove(self, grant_id: str) -> bool: ...
    def get(self, grant_id: str) -> TemporaryGrant | None: ...
    def active(self, now: datetime) -> list[TemporaryGrant]: ...
    def remove_scope_prefix(self, prefix: str) -> int: ...


class GrantRepository(Protocol):
    """Persistence adapter (Data Model `temporary_grants`); implemented by the memory/db package."""

    def load_all(self) -> list[TemporaryGrant]: ...
    def save(self, grant: TemporaryGrant) -> None: ...
    def delete(self, grant_id: str) -> None: ...


class InMemoryGrantStore:
    def __init__(self, grants: Iterable[TemporaryGrant] = ()) -> None:
        self._grants: dict[str, TemporaryGrant] = {g.grant_id: g for g in grants}

    def add(self, grant: TemporaryGrant) -> None:
        self._grants[grant.grant_id] = grant

    def remove(self, grant_id: str) -> bool:
        return self._grants.pop(grant_id, None) is not None

    def get(self, grant_id: str) -> TemporaryGrant | None:
        return self._grants.get(grant_id)

    def active(self, now: datetime) -> list[TemporaryGrant]:
        expired = [gid for gid, g in self._grants.items() if g.expires_at <= now]
        for gid in expired:
            del self._grants[gid]
        return list(self._grants.values())

    def remove_scope_prefix(self, prefix: str) -> int:
        doomed = [gid for gid, g in self._grants.items() if g.scope.startswith(prefix)]
        for gid in doomed:
            del self._grants[gid]
        return len(doomed)


class RepositoryGrantStore(InMemoryGrantStore):
    """In-memory cache in front of a `GrantRepository` (loads on construction, writes through)."""

    def __init__(self, repository: GrantRepository) -> None:
        self._repository = repository
        super().__init__(repository.load_all())

    def add(self, grant: TemporaryGrant) -> None:
        super().add(grant)
        self._repository.save(grant)

    def remove(self, grant_id: str) -> bool:
        removed = super().remove(grant_id)
        if removed:
            self._repository.delete(grant_id)
        return removed

    def active(self, now: datetime) -> list[TemporaryGrant]:
        before = set(self._grants)
        alive = super().active(now)
        for gid in before.difference(self._grants):
            self._repository.delete(gid)
        return alive

    def remove_scope_prefix(self, prefix: str) -> int:
        doomed = [gid for gid, g in self._grants.items() if g.scope.startswith(prefix)]
        for gid in doomed:
            self.remove(gid)
        return len(doomed)


# ---- matching helpers ----------------------------------------------------------------------------


def glob_match(value: str, pattern: str) -> bool:
    """Case-insensitive glob; "*" matches everything including the empty string."""
    if pattern == "*":
        return True
    return fnmatchcase(value.lower(), pattern.lower())


def matches_any(value: str, patterns: Sequence[str]) -> bool:
    return any(glob_match(value, p) for p in patterns)


def normalise_path(value: str) -> str:
    text = value.strip().replace("\\", "/").lower()
    if not text:
        return ""
    normalised = posixpath.normpath(text)
    return normalised.rstrip("/") if len(normalised) > 1 else normalised


def looks_like_path(target: str) -> bool:
    return bool(target) and ("/" in target or "\\" in target or bool(_DRIVE.match(target)))


def under_roots(target: str, roots: Sequence[str]) -> bool:
    path = normalise_path(target)
    for root in roots:
        root_n = normalise_path(root)
        if root_n and (path == root_n or path.startswith(root_n + "/")):
            return True
    return False


def rule_matches(rule: ProfileRule, request: PermissionRequest) -> bool:
    target = request.target.replace("\\", "/")
    return (
        glob_match(request.agent, rule.agent)
        and glob_match(request.tool, rule.tool)
        and glob_match(request.action, rule.action)
        and glob_match(request.mode, rule.mode)
        and glob_match(request.risk.value, rule.risk)
        and glob_match(target, rule.target.replace("\\", "/"))
    )


def is_memory_write(request: PermissionRequest) -> bool:
    return (
        matches_any(request.tool, MEMORY_TOOL_PATTERNS)
        and request.risk is not Risk.READ
        and request.action.lower() not in READ_ACTIONS
    )


def is_capture(request: PermissionRequest) -> bool:
    return matches_any(request.tool, CAPTURE_TOOL_PATTERNS)


def is_filesystem(request: PermissionRequest) -> bool:
    return matches_any(request.tool, FILESYSTEM_TOOL_PATTERNS)


def is_screenshot_to_cloud(request: PermissionRequest) -> bool:
    if not matches_any(request.tool, SCREENSHOT_TOOL_PATTERNS):
        return False
    return matches_any(request.action, CLOUD_ACTION_PATTERNS) or request.target.lower().startswith(
        "http"
    )


def grant_matches(grant: TemporaryGrant, request: PermissionRequest, now: datetime) -> bool:
    """Scope grammar: task:<id> | project:<id> | repo:<path> | session:<tool-glob>/<action-glob>."""
    if grant.expires_at <= now:
        return False
    if request.risk is Risk.CRITICAL or RISK_ORDER[request.risk] > RISK_ORDER[grant.max_risk]:
        return False
    kind, _, value = grant.scope.partition(":")
    if not value:
        return False
    if kind == "task":
        return request.task_id == value
    if kind == "project":
        return request.target.startswith(value) or (request.task_id or "").startswith(value)
    if kind == "repo":
        return looks_like_path(request.target) and under_roots(request.target, [value])
    if kind == "session":
        tool_glob, _, action_glob = value.partition("/")
        return glob_match(request.tool, tool_glob) and glob_match(
            request.action, action_glob or "*"
        )
    return False


# ---- engine --------------------------------------------------------------------------------------


@dataclass
class _Pending:
    request: PermissionRequest
    future: asyncio.Future[tuple[Decision, bool, str]]
    created_at: datetime


class DefaultPermissionEngine:
    """Implements `nox.security.model.PermissionEngine`."""

    def __init__(
        self,
        *,
        profiles: ProfileProvider,
        privacy: PrivacyStateProvider,
        grants: GrantStore | None = None,
        audit: AuditLog | None = None,
        bus: EventBus | None = None,
        clock: Clock | None = None,
        initial_profile: str = "companion",
        confirm_timeout_s: float = 60.0,
        session_grant_ttl_s: float = 8 * 3600,
        cloud_tool_patterns: Sequence[str] = CLOUD_TOOL_PATTERNS,
        session_id: str | None = None,
    ) -> None:
        self._profiles = profiles
        self._privacy = privacy
        self._grants: GrantStore = grants if grants is not None else InMemoryGrantStore()
        self._audit = audit
        self._bus = bus
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._confirm_timeout_s = confirm_timeout_s
        self._session_grant_ttl = timedelta(seconds=session_grant_ttl_s)
        self._cloud_tool_patterns = tuple(cloud_tool_patterns)
        self._session_id = session_id or uuid.uuid4().hex[:12]
        self._profile: Profile = profiles.get(initial_profile)
        self._pending: dict[str, _Pending] = {}

    # ---- PermissionEngine protocol ---------------------------------------------------------------

    def check(self, request: PermissionRequest) -> PermissionResult:
        snapshot = self._privacy.snapshot()
        now = self._clock()
        result = self.evaluate(request, self._profile, snapshot, self._grants.active(now), now)
        self._record(request, result, request_id=uuid.uuid4().hex, by="policy", snapshot=snapshot)
        return result

    def active_profile(self) -> Profile:
        return self._profile

    def set_profile(self, profile_id: str, *, by: str) -> None:
        profile = self._profiles.get(profile_id)  # KeyError / ProfileError propagate
        previous = self._profile.id
        self._profile = profile
        self._audit_append(
            actor=by,
            tool="security",
            action="profile.set",
            target=profile_id,
            decision=Decision.ALLOW.value,
            result="ok",
            details={"previous": previous},
        )
        log.info("security.profile_changed", previous=previous, current=profile_id, by=by)

    def grant_temporary(self, grant: TemporaryGrant) -> None:
        if grant.max_risk is Risk.CRITICAL:
            raise ValueError("temporary grants can never cover critical risk")
        self._grants.add(grant)
        self._audit_append(
            actor=grant.granted_by,
            tool="security",
            action="grant.create",
            target=grant.scope,
            decision=Decision.ALLOW.value,
            result="ok",
            details={
                "grant_id": grant.grant_id,
                "max_risk": grant.max_risk.value,
                "expires_at": grant.expires_at.isoformat(),
            },
        )

    def revoke(self, grant_id: str) -> None:
        removed = self._grants.remove(grant_id)
        self._audit_append(
            actor="user",
            tool="security",
            action="grant.revoke",
            target=grant_id,
            decision=Decision.ALLOW.value,
            result="ok" if removed else "failed",
        )

    # ---- pure evaluation -------------------------------------------------------------------------

    def is_cloud_tool(self, request: PermissionRequest) -> bool:
        return matches_any(request.tool, self._cloud_tool_patterns) or matches_any(
            request.action, CLOUD_ACTION_PATTERNS
        )

    def evaluate(
        self,
        request: PermissionRequest,
        profile: Profile,
        snapshot: PrivacySnapshot,
        grants: Sequence[TemporaryGrant],
        now: datetime,
    ) -> PermissionResult:
        """Deterministic decision; no I/O, no events (see module docstring for the order)."""
        hard = hard_prohibition_for(request.tool, request.action)
        if hard is not None:
            return PermissionResult(
                decision=Decision.DENY,
                rule_id=f"hard.{hard}",
                reason="hard prohibition; not overridable by any layer",
            )

        if (snapshot.safe_mode or snapshot.panic) and request.risk is not Risk.READ:
            return PermissionResult(
                decision=Decision.DENY,
                rule_id="safe_mode",
                reason="kill switch engaged: no side effects until manual resume",
            )

        cloud = self.is_cloud_tool(request)
        memory_write = is_memory_write(request)
        if snapshot.mode in (PrivacyMode.PRIVATE, PrivacyMode.OFFLINE) and cloud:
            return PermissionResult(
                decision=Decision.DENY,
                rule_id=f"privacy.{snapshot.mode.value}.cloud",
                reason=f"cloud tools are disabled in privacy mode {snapshot.mode.value}",
            )
        if snapshot.mode is PrivacyMode.PRIVATE and memory_write:
            return PermissionResult(
                decision=Decision.DENY,
                rule_id="privacy.private.memory_write",
                reason="PRIVATE mode keeps everything session-only",
            )
        if snapshot.zone_active and (is_capture(request) or memory_write):
            return PermissionResult(
                decision=Decision.DENY,
                rule_id="privacy.zone_active",
                reason="a privacy zone is in the foreground",
            )

        if request.risk is Risk.CRITICAL:
            return PermissionResult(
                decision=Decision.DENY,
                rule_id="risk.critical",
                requires_pin=True,
                reason="critical actions need the PIN flow, never a tool call",
            )

        for grant in grants:
            if grant_matches(grant, request, now):
                return PermissionResult(
                    decision=Decision.ALLOW,
                    rule_id=f"grant.{grant.scope}",
                    reason="temporary grant",
                    grant_id=grant.grant_id,
                )

        if not profile.cloud_allowed and cloud:
            return PermissionResult(
                decision=Decision.DENY,
                rule_id=f"{profile.id}.cloud_disabled",
                reason="profile forbids cloud tools",
            )
        if not profile.memory_writes_allowed and memory_write:
            return PermissionResult(
                decision=Decision.DENY,
                rule_id=f"{profile.id}.memory_writes_disabled",
                reason="profile forbids memory and vault writes",
            )
        if not profile.screenshots_to_cloud and is_screenshot_to_cloud(request):
            return PermissionResult(
                decision=Decision.DENY,
                rule_id=f"{profile.id}.screenshots_to_cloud",
                reason="profile forbids sending screenshots to the cloud",
            )
        if profile.tools_allowed and not matches_any(request.tool, profile.tools_allowed):
            return PermissionResult(
                decision=Decision.DENY,
                rule_id=f"{profile.id}.tool_not_allowed",
                reason="tool is not in the profile allow-list",
            )
        if (
            profile.filesystem_roots
            and is_filesystem(request)
            and looks_like_path(request.target)
            and not under_roots(request.target, profile.filesystem_roots)
        ):
            return PermissionResult(
                decision=Decision.DENY,
                rule_id=f"{profile.id}.filesystem_roots",
                reason="target is outside the profile's filesystem roots",
            )

        for rule in profile.rules:
            if rule_matches(rule, request):
                return PermissionResult(decision=rule.decision, rule_id=rule.id, reason=rule.reason)

        decision = DEFAULT_BY_RISK[request.risk]
        return PermissionResult(
            decision=decision, rule_id=f"default.{request.risk.value}", reason="default by risk"
        )

    # ---- confirmation flow -----------------------------------------------------------------------

    def request_confirmation(self, request: PermissionRequest) -> str:
        """Register a pending confirmation, emit `security.permission_requested`, return its
        grant_id."""
        loop = asyncio.get_running_loop()
        grant_id = uuid.uuid4().hex
        self._pending[grant_id] = _Pending(
            request=request, future=loop.create_future(), created_at=self._clock()
        )
        publish_nowait(
            self._bus,
            E.SECURITY_PERMISSION_REQUESTED,
            PermissionRequested(
                request_id=grant_id,
                agent=request.agent,
                tool=request.tool,
                action=request.action,
                mode=request.mode,
                risk=request.risk.value,
                target=request.target,
            ),
            corr=grant_id,
        )
        return grant_id

    def reply(
        self, grant_id: str, decision: Decision, *, remember: bool = False, by: str = "user"
    ) -> bool:
        """Resolve a pending confirmation; False when unknown, already decided or timed out."""
        pending = self._pending.get(grant_id)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result((decision, remember, by))
        return True

    def pending(self) -> list[str]:
        return list(self._pending)

    async def await_confirmation(
        self, grant_id: str, *, timeout: float | None = None
    ) -> PermissionResult:
        pending = self._pending[grant_id]
        request = pending.request
        wait = self._confirm_timeout_s if timeout is None else timeout
        try:
            decision, remember, by = await asyncio.wait_for(pending.future, wait)
        except TimeoutError:
            decision, remember, by = Decision.DENY, False, "policy"
            rule_id = "confirm.timeout"
            reason = f"no answer within {wait:g} s"
        else:
            rule_id = "confirm.user" if decision is Decision.ALLOW else "confirm.denied"
            reason = "confirmed by user" if decision is Decision.ALLOW else "denied by user"
        finally:
            self._pending.pop(grant_id, None)

        final = Decision.ALLOW if decision is Decision.ALLOW else Decision.DENY
        result_grant_id: str | None = None
        if final is Decision.ALLOW and remember:
            grant = TemporaryGrant(
                grant_id=grant_id,
                scope=f"session:{request.tool}/{request.action or '*'}",
                max_risk=request.risk,
                expires_at=self._clock() + self._session_grant_ttl,
                granted_by=by,
            )
            self.grant_temporary(grant)
            result_grant_id = grant_id
        result = PermissionResult(
            decision=final, rule_id=rule_id, reason=reason, grant_id=result_grant_id
        )
        self._record(request, result, request_id=grant_id, by=by, snapshot=self._privacy.snapshot())
        await publish(
            self._bus,
            E.SECURITY_PERMISSION_DECIDED,
            PermissionDecided(
                request_id=grant_id, decision=final.value, rule_id=rule_id, by=by, reason=reason
            ),
            corr=grant_id,
        )
        return result

    async def authorize(self, request: PermissionRequest) -> PermissionResult:
        """check() plus the confirmation round-trip; returns only ALLOW or DENY."""
        result = self.check(request)
        if result.decision is not Decision.CONFIRM:
            return result
        grant_id = self.request_confirmation(request)
        return await self.await_confirmation(grant_id)

    def end_session(self) -> int:
        """Revoke every session-scoped grant ("remember for this session")."""
        removed = self._grants.remove_scope_prefix("session:")
        if removed:
            self._audit_append(
                actor="system",
                tool="security",
                action="grant.session_end",
                target="",
                decision=Decision.ALLOW.value,
                result="ok",
                details={"removed": str(removed)},
            )
        return removed

    # ---- recording -------------------------------------------------------------------------------

    def _audit_append(self, **kwargs: object) -> None:
        if self._audit is None:
            return
        self._audit.append(**kwargs)  # type: ignore[arg-type]

    def _record(
        self,
        request: PermissionRequest,
        result: PermissionResult,
        *,
        request_id: str,
        by: str,
        snapshot: PrivacySnapshot,
    ) -> None:
        outcome = {Decision.ALLOW: "ok", Decision.CONFIRM: "pending", Decision.DENY: "denied"}[
            result.decision
        ]
        target = "<redacted>" if snapshot.zone_active else request.target
        self._audit_append(
            actor=request.agent,
            tool=request.tool,
            action=request.action,
            target=target,
            decision=result.decision.value,
            result=outcome,
            task_id=request.task_id,
            details={
                "rule_id": result.rule_id,
                "risk": request.risk.value,
                "mode": request.mode,
                "origin": request.origin,
                "privacy": snapshot.mode.value,
                "by": by,
                "request_id": request_id,
            },
        )
        if by == "policy":
            publish_nowait(
                self._bus,
                E.SECURITY_PERMISSION_DECIDED,
                PermissionDecided(
                    request_id=request_id,
                    decision=result.decision.value,
                    rule_id=result.rule_id,
                    by=by,
                    reason=result.reason,
                ),
                corr=request_id,
            )
        log.debug(
            "security.permission_decided",
            tool=request.tool,
            action=request.action,
            decision=result.decision.value,
            rule_id=result.rule_id,
        )
