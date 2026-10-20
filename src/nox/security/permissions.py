"""The permission engine: every tool call passes through it before it runs.

`evaluate()` is the whole policy, and it is pure - given a request, the active profile, a privacy
snapshot, the live grants and the time, it always returns the same decision, with no I/O and no
events. The guards in `EVALUATION_GUARDS` run in order and the first one that answers wins: hard
prohibitions, safe mode, the privacy constraints, critical risk, temporary grants, the profile's
restrictions, the profile's rules, and finally the default for the request's risk level. A new
rule is a new guard function in that list, not another branch in a long function.

`check()` wraps `evaluate()` with auditing. Confirmation is a second step:
`request_confirmation()` registers the question and emits it, `reply()` answers it, and
`await_confirmation()` waits (a timeout denies). `remember=True` turns an allow into a
session-scoped grant for that tool and action - except for high risk, which is asked every time.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from nox.core.events import E, EventBus, PermissionDecided, PermissionRequested
from nox.core.globbing import value_matches, value_matches_any
from nox.core.state import PrivacyMode
from nox.security._events import publish, publish_nowait
from nox.security._logging import get_logger
from nox.security.audit_sink import SafeAuditLog
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
from nox.security.taint import LookupKind, TaintLedger

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
    "mic",
    "mic.*",
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
    #: Capture kinds ("microphone", "camera", "screen") the user switched off
    #: (`privacy.capture.*`). A tool of that kind is refused below the model.
    capture_disabled: frozenset[str] = frozenset()


#: Tool-id patterns per capture kind, for the `privacy.capture.*` switches.
CAPTURE_KIND_PATTERNS: dict[str, tuple[str, ...]] = {
    "microphone": ("microphone", "microphone.*", "mic", "mic.*"),
    "camera": ("camera", "camera.*"),
    "screen": ("screen", "screen.*", "screenshot", "screenshot.*", "capture", "capture.*"),
}


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


#: Risk levels a "remember for this session" answer never turns into a grant: a hard-to-reverse
#: action (deleting a scene, starting a broadcast) is confirmed every single time.
NEVER_REMEMBERED: frozenset[Risk] = frozenset({Risk.HIGH, Risk.CRITICAL})


def never_remembered(risk: Risk) -> bool:
    """Whether a confirmed request of this risk is asked again next time, whatever the reply."""
    return risk in NEVER_REMEMBERED


# ---- matching helpers ----------------------------------------------------------------------------


def normalise_path(value: str) -> str:
    """Fold a filesystem target onto one comparable form: forward slashes, lowercase, no `..`."""
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


def target_matches(target: str, pattern: str) -> bool:
    """Match a rule's `target` glob, normalising both sides when the target is a filesystem path.

    Without the normalisation, `C:/Users/me/../../Windows/System32` matched a rule scoped to
    `C:/Users/*` - the very traversal `under_roots` has always normalised away. URLs keep their
    literal form, because collapsing `https://` would break every host rule.
    """
    if looks_like_path(target) and not target.lower().startswith(("http://", "https://")):
        return value_matches(normalise_path(target), normalise_path(pattern))
    return value_matches(target.replace("\\", "/"), pattern.replace("\\", "/"))


def rule_matches(rule: ProfileRule, request: PermissionRequest) -> bool:
    return (
        value_matches(request.agent, rule.agent)
        and value_matches(request.tool, rule.tool)
        and value_matches(request.action, rule.action)
        and value_matches(request.mode, rule.mode)
        and value_matches(request.risk.value, rule.risk)
        and target_matches(request.target, rule.target)
    )


def is_memory_write(request: PermissionRequest) -> bool:
    return (
        value_matches_any(request.tool, MEMORY_TOOL_PATTERNS)
        and request.risk is not Risk.READ
        and request.action.lower() not in READ_ACTIONS
    )


def is_capture(request: PermissionRequest) -> bool:
    return value_matches_any(request.tool, CAPTURE_TOOL_PATTERNS)


def is_filesystem(request: PermissionRequest) -> bool:
    return value_matches_any(request.tool, FILESYSTEM_TOOL_PATTERNS)


def is_screenshot_to_cloud(request: PermissionRequest) -> bool:
    if not value_matches_any(request.tool, SCREENSHOT_TOOL_PATTERNS):
        return False
    return value_matches_any(
        request.action, CLOUD_ACTION_PATTERNS
    ) or request.target.lower().startswith("http")


def is_scoped_prefix(value: str, prefix: str) -> bool:
    """`value` is `prefix` itself or lies below it - never merely starts with the same letters.

    A grant scoped to project `a` used to match every target beginning with `a`, `api-keys`
    included.
    """
    return bool(prefix) and (value == prefix or value.startswith(prefix + "/"))


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
        return is_scoped_prefix(request.target, value) or is_scoped_prefix(
            request.task_id or "", value
        )
    if kind == "repo":
        return looks_like_path(request.target) and under_roots(request.target, [value])
    if kind == "session":
        tool_glob, _, action_glob = value.partition("/")
        return value_matches(request.tool, tool_glob) and value_matches(
            request.action, action_glob or "*"
        )
    if kind == "cmd":
        # A remembered shell command pattern (argv0 + subcommand). `shell.run` encodes a simple,
        # non-curated command as target `cmd:<pattern>`; a command with a shell operator, an env
        # prefix or an interpreter `-c` is encoded differently and so can never equal this scope.
        return request.tool == "shell" and request.target == f"cmd:{value}"
    return False


# ---- evaluation guards ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    """Everything a guard may look at. Frozen, so a guard cannot influence the ones after it."""

    request: PermissionRequest
    profile: Profile
    snapshot: PrivacySnapshot
    grants: Sequence[TemporaryGrant]
    now: datetime
    #: Pre-computed, because four guards ask the same two questions.
    cloud: bool
    memory_write: bool
    #: True once a tool result Nox did not author entered this request's conversation
    #: (`nox.security.taint`). While it is, a remembered grant above LOW no longer silently allows,
    #: and an allowed call that sends data off the machine is turned into a confirmation.
    tainted: bool = False
    #: True when the conversation is tainted but this call is a read-only lookup whose free text
    #: is demonstrably taken from what the user said in this conversation, not from untrusted
    #: content (`TaintLedger.user_directed`). Such a call is treated as if it were clean.
    user_directed: bool = False


#: A guard answers the request, or returns None to let the next one decide.
Guard = Callable[[EvaluationContext], PermissionResult | None]


def _hard_prohibition(ctx: EvaluationContext) -> PermissionResult | None:
    hard = hard_prohibition_for(ctx.request.tool, ctx.request.action)
    if hard is None:
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id=f"hard.{hard}",
        reason="hard prohibition; not overridable by any layer",
    )


def _safe_mode(ctx: EvaluationContext) -> PermissionResult | None:
    if not (ctx.snapshot.safe_mode or ctx.snapshot.panic) or ctx.request.risk is Risk.READ:
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id="safe_mode",
        reason="kill switch engaged: no side effects until manual resume",
    )


def _privacy_cloud(ctx: EvaluationContext) -> PermissionResult | None:
    mode = ctx.snapshot.mode
    if mode not in (PrivacyMode.PRIVATE, PrivacyMode.OFFLINE) or not ctx.cloud:
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id=f"privacy.{mode.value}.cloud",
        reason=f"cloud tools are disabled in privacy mode {mode.value}",
    )


def _privacy_memory_write(ctx: EvaluationContext) -> PermissionResult | None:
    if ctx.snapshot.mode is not PrivacyMode.PRIVATE or not ctx.memory_write:
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id="privacy.private.memory_write",
        reason="PRIVATE mode keeps everything session-only",
    )


def _privacy_zone(ctx: EvaluationContext) -> PermissionResult | None:
    if not ctx.snapshot.zone_active or not (is_capture(ctx.request) or ctx.memory_write):
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id="privacy.zone_active",
        reason="a privacy zone is in the foreground",
    )


def capture_kind(request: PermissionRequest) -> str | None:
    for kind, patterns in CAPTURE_KIND_PATTERNS.items():
        if value_matches_any(request.tool, patterns):
            return kind
    return None


def _privacy_capture_flag(ctx: EvaluationContext) -> PermissionResult | None:
    kind = capture_kind(ctx.request)
    if kind is None or kind not in ctx.snapshot.capture_disabled:
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id=f"privacy.capture.{kind}",
        reason=f"{kind} capture is switched off in the privacy settings",
    )


def _critical_risk(ctx: EvaluationContext) -> PermissionResult | None:
    if ctx.request.risk is not Risk.CRITICAL:
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id="risk.critical",
        requires_pin=True,
        reason="critical actions need the PIN flow, never a tool call",
    )


def _temporary_grant(ctx: EvaluationContext) -> PermissionResult | None:
    for grant in ctx.grants:
        if not grant_matches(grant, ctx.request, ctx.now):
            continue
        if ctx.tainted and RISK_ORDER[ctx.request.risk] > RISK_ORDER[Risk.LOW]:
            # Untrusted content is in the turn; a remembered grant above LOW must not act on its
            # own. Skip it so the request falls through to a fresh confirmation.
            log.info("security.taint_grant_fallback", tool=ctx.request.tool, scope=grant.scope)
            return None
        return PermissionResult(
            decision=Decision.ALLOW,
            rule_id=f"grant.{grant.scope}",
            reason="temporary grant",
            grant_id=grant.grant_id,
        )
    return None


def _shell_curated_readonly(ctx: EvaluationContext) -> PermissionResult | None:
    """A curated, read-only shell command runs in the sandbox without a confirmation.

    `shell.run` classifies the command and, when it is one of the vetted read-only commands given in
    `argv` form (`df`, `ls`/`dir`, `git status`, `uname`, ...), sets the target to
    `readonly:<pattern>`. Only `shell` can produce that prefix - the target comes from the tool's
    own `targets()`, not the model - so no other tool can borrow this allowance. Confirmation
    fatigue is a UX bug, and these commands change nothing.

    This guard runs **last**, after every prohibition, privacy, safe-mode, grant and profile guard,
    so it can only lift the HIGH *default* for `shell.run` - never an explicit profile DENY/CONFIRM
    (those return first), and never privacy, safe mode or the kill switch. If a profile has any rule
    that decides `shell`, that rule has already answered and this is not reached. A tainted turn
    drops the exception too.
    """
    if ctx.request.tool != "shell" or not ctx.request.target.startswith("readonly:"):
        return None
    if ctx.tainted and not ctx.user_directed:
        return None
    return PermissionResult(
        decision=Decision.ALLOW,
        rule_id="shell.curated_readonly",
        reason="curated read-only command, run in the sandbox without confirmation",
    )


def _profile_cloud(ctx: EvaluationContext) -> PermissionResult | None:
    if ctx.profile.cloud_allowed or not ctx.cloud:
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id=f"{ctx.profile.id}.cloud_disabled",
        reason="profile forbids cloud tools",
    )


def _profile_memory_writes(ctx: EvaluationContext) -> PermissionResult | None:
    if ctx.profile.memory_writes_allowed or not ctx.memory_write:
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id=f"{ctx.profile.id}.memory_writes_disabled",
        reason="profile forbids memory and vault writes",
    )


def _profile_screenshots_to_cloud(ctx: EvaluationContext) -> PermissionResult | None:
    if ctx.profile.screenshots_to_cloud or not is_screenshot_to_cloud(ctx.request):
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id=f"{ctx.profile.id}.screenshots_to_cloud",
        reason="profile forbids sending screenshots to the cloud",
    )


def _profile_tool_allowlist(ctx: EvaluationContext) -> PermissionResult | None:
    allowed = ctx.profile.tools_allowed
    if not allowed or value_matches_any(ctx.request.tool, allowed):
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id=f"{ctx.profile.id}.tool_not_allowed",
        reason="tool is not in the profile allow-list",
    )


def _profile_filesystem_roots(ctx: EvaluationContext) -> PermissionResult | None:
    roots = ctx.profile.filesystem_roots
    target = ctx.request.target
    if not roots or not is_filesystem(ctx.request) or not looks_like_path(target):
        return None
    if under_roots(target, roots):
        return None
    return PermissionResult(
        decision=Decision.DENY,
        rule_id=f"{ctx.profile.id}.filesystem_roots",
        reason="target is outside the profile's filesystem roots",
    )


def _profile_rules(ctx: EvaluationContext) -> PermissionResult | None:
    for rule in ctx.profile.rules:
        if rule_matches(rule, ctx.request):
            return PermissionResult(decision=rule.decision, rule_id=rule.id, reason=rule.reason)
    return None


#: The policy, in order. The first guard that answers decides; if none does, the request falls
#: through to `DEFAULT_BY_RISK`.
EVALUATION_GUARDS: tuple[Guard, ...] = (
    _hard_prohibition,
    _safe_mode,
    _privacy_cloud,
    _privacy_memory_write,
    _privacy_zone,
    _privacy_capture_flag,
    _critical_risk,
    _temporary_grant,
    _profile_cloud,
    _profile_memory_writes,
    _profile_screenshots_to_cloud,
    _profile_tool_allowlist,
    _profile_filesystem_roots,
    _profile_rules,
    # Last: the curated read-only exception may only lift the HIGH default for shell.run, never an
    # explicit profile DENY/CONFIRM (which returned above), nor privacy/safe-mode/kill-switch.
    _shell_curated_readonly,
)


def _lookup_kind(request: PermissionRequest) -> LookupKind | None:
    """Which read-only lookup this is, if it is one that may be user-directed. Only these three:
    a search goes to the user's own configured backend, a fetch only to a URL the user typed, and
    a curated command changes nothing and runs in the strong sandbox."""
    if request.tool == "web" and request.action in ("search", "fetch"):
        return "search" if request.action == "search" else "fetch"
    if request.tool == "shell" and request.target.startswith("readonly:"):
        return "shell"
    return None


def tainted_escalation(ctx: EvaluationContext, result: PermissionResult) -> PermissionResult:
    """Turn an ALLOW into a CONFIRM when the conversation is tainted and the call could act on
    injected instructions without the user seeing it.

    Applied after the guards, so it can only tighten: a DENY stays a DENY, a CONFIRM stays one.
    Two cases: a call that sends data off the machine (a cloud/network tool such as `web.fetch`,
    or a declared egress) - injected text could have chosen the URL or query to exfiltrate what is
    in the context - and the curated read-only shell exception, which exists to save a click, not
    to let a web page run commands.

    A user-directed lookup (`EvaluationContext.user_directed`) is exempt: its query, URL or
    arguments come from what the user said this turn, so injected text did not choose them.
    """
    if not ctx.tainted or ctx.user_directed or result.decision is not Decision.ALLOW:
        return result
    request = ctx.request
    if ctx.cloud or request.network:
        reason = "the conversation holds untrusted content and this call sends data out"
        return PermissionResult(decision=Decision.CONFIRM, rule_id="taint.egress", reason=reason)
    if request.tool == "shell" and request.target.startswith("readonly:"):
        reason = "the conversation holds untrusted content; even read-only commands ask"
        return PermissionResult(
            decision=Decision.CONFIRM, rule_id="taint.shell_readonly", reason=reason
        )
    return result


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
        self._audit = SafeAuditLog(audit)
        self._bus = bus
        self._clock: Clock = clock or (lambda: datetime.now(UTC))
        self._confirm_timeout_s = confirm_timeout_s
        self._session_grant_ttl = timedelta(seconds=session_grant_ttl_s)
        self._cloud_tool_patterns = tuple(cloud_tool_patterns)
        self._session_id = session_id or uuid.uuid4().hex[:12]
        self._profile: Profile = profiles.get(initial_profile)
        self._pending: dict[str, _Pending] = {}
        #: Conversations (`PermissionRequest.conversation`) that hold untrusted content, and
        #: what the user said in each.
        self._taint = TaintLedger()

    # ---- PermissionEngine protocol ---------------------------------------------------------------

    def mark_tainted(self, conversation: str, content: str | None = None) -> None:
        """Record that `conversation` now holds content Nox did not author.

        The tool loop calls this after every tool result that is not trusted-internal, with the
        result's text, and the orchestrator the moment the provider reports a tool of its own,
        with `None` (not readable: nothing in the conversation is user-directed any more). It
        stays set until `clear_taint` - when the orchestrator discards that conversation's
        context - never merely because a new turn began.
        """
        if self._taint.mark(conversation, content):
            log.info("security.taint_changed", tainted=True)

    def clear_taint(self, conversation: str) -> None:
        """The conversation's context was discarded: nothing untrusted is in it any more."""
        if self._taint.clear(conversation):
            log.info("security.taint_changed", tainted=False)

    def is_tainted(self, conversation: str) -> bool:
        return self._taint.is_tainted(conversation)

    def note_user_message(self, conversation: str, text: str) -> None:
        """What the user said in a turn of `conversation` (the source `user_directed` accepts)."""
        self._taint.note_user_message(conversation, text)

    def _user_directed(self, request: PermissionRequest) -> bool:
        kind = _lookup_kind(request)
        if kind is None:
            return False
        return self._taint.user_directed(request.conversation, kind, request.lookup)

    def check(self, request: PermissionRequest) -> PermissionResult:
        snapshot = self._privacy.snapshot()
        now = self._clock()
        result = self.evaluate(request, self._profile, snapshot, self._grants.active(now), now)
        self._record(request, result, request_id=uuid.uuid4().hex, by="policy", snapshot=snapshot)
        return result

    def preview(self, request: PermissionRequest) -> PermissionResult:
        """The decision this request would get, without recording it.

        `check` is the real thing and leaves an audit entry, which is right for a decision that
        precedes an action. The capability catalogue asks about every tool at once and takes no
        action at all; auditing that would bury the log it exists to explain.
        """
        now = self._clock()
        return self.evaluate(
            request, self._profile, self._privacy.snapshot(), self._grants.active(now), now
        )

    def active_profile(self) -> Profile:
        return self._profile

    def set_profile(self, profile_id: str, *, by: str) -> None:
        profile = self._profiles.get(profile_id)  # KeyError / ProfileError propagate
        previous = self._profile.id
        self._profile = profile
        self._audit.append(
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
        self._audit.append(
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
        self._audit.append(
            actor="user",
            tool="security",
            action="grant.revoke",
            target=grant_id,
            decision=Decision.ALLOW.value,
            result="ok" if removed else "failed",
        )

    # ---- pure evaluation -------------------------------------------------------------------------

    def is_cloud_tool(self, request: PermissionRequest) -> bool:
        # A call that declares it sends data out (`ToolSpec.network`: shell egress, a package
        # download) is a cloud call for every privacy rule, whatever its name.
        return (
            request.network
            or value_matches_any(request.tool, self._cloud_tool_patterns)
            or value_matches_any(request.action, CLOUD_ACTION_PATTERNS)
        )

    def evaluate(
        self,
        request: PermissionRequest,
        profile: Profile,
        snapshot: PrivacySnapshot,
        grants: Sequence[TemporaryGrant],
        now: datetime,
    ) -> PermissionResult:
        """Deterministic decision; no I/O, no events. Guards run in `EVALUATION_GUARDS` order."""
        tainted = self.is_tainted(request.conversation)
        context = EvaluationContext(
            request=request,
            profile=profile,
            snapshot=snapshot,
            grants=grants,
            now=now,
            cloud=self.is_cloud_tool(request),
            memory_write=is_memory_write(request),
            tainted=tainted,
            user_directed=tainted and self._user_directed(request),
        )
        for guard in EVALUATION_GUARDS:
            result = guard(context)
            if result is not None:
                return tainted_escalation(context, result)
        return tainted_escalation(
            context,
            PermissionResult(
                decision=DEFAULT_BY_RISK[request.risk],
                rule_id=f"default.{request.risk.value}",
                reason="default by risk",
            ),
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
            self._requested(grant_id, self._pending[grant_id]),
            corr=grant_id,
        )
        return grant_id

    def _requested(self, grant_id: str, pending: _Pending) -> PermissionRequested:
        request = pending.request
        expires = pending.created_at + timedelta(seconds=self._confirm_timeout_s)
        return PermissionRequested(
            request_id=grant_id,
            agent=request.agent,
            tool=request.tool,
            action=request.action,
            mode=request.mode,
            risk=request.risk.value,
            target=request.target,
            expires_at=expires.isoformat(),
            remember_ttl_s=self._session_grant_ttl.total_seconds(),
        )

    def pending_requests(self) -> list[PermissionRequested]:
        """The questions still waiting for an answer, as their `permission_requested` events - for
        a dashboard that connects (or reconnects) while one is open."""
        return [
            self._requested(grant_id, pending)
            for grant_id, pending in self._pending.items()
            if not pending.future.done()
        ]

    def pending_request(self, grant_id: str) -> PermissionRequest | None:
        pending = self._pending.get(grant_id)
        return None if pending is None or pending.future.done() else pending.request

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
        scope = self._remember_scope(request)
        # Both rules: only a rememberable scope, and never for high risk (asked every time).
        if (
            final is Decision.ALLOW
            and remember
            and scope is not None
            and not never_remembered(request.risk)
        ):
            grant = TemporaryGrant(
                grant_id=grant_id,
                scope=scope,
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

    def _remember_scope(self, request: PermissionRequest) -> str | None:
        """The grant scope a "remember" would create for this request, or None for "never".

        `shell.run` remembers one command *pattern* (`cmd:git status`) and only for a simple
        command, never the whole tool - a complex command (target `complex:...`) or a curated
        read-only one (`readonly:...`) produces no grant. Every other rememberable tool gets the
        session scope. A tool marked `rememberable=False` (`package.*`, `mic.record_long`,
        `skill.*`) never produces one.
        """
        if not request.rememberable:
            return None
        if request.tool == "shell":
            return request.target if request.target.startswith("cmd:") else None
        return f"session:{request.tool}/{request.action or '*'}"

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
            self._audit.append(
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
        self._audit.append(
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
