"""Builds the honest answer to "what can you do right now?".

The report joins three sources that until now nobody asked together:

* the **tool registry**, for what exists,
* the **permission engine**, asked through `preview` so that describing the world leaves no audit
  entry for an action nobody took,
* and the **gap table**, for what does not exist at all.

One deliberate limitation, stated rather than hidden: the preview asks with an empty target, so a
tool whose decision depends on what it is pointed at gets the decision for "no particular target".
Those capabilities carry `target_dependent=True` and the caller has to say so. Pretending a single
yes covers every path would be the exact dishonesty this module exists to remove.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from fnmatch import fnmatch
from typing import Protocol

from nox.capabilities.gaps import CURATED_GAPS, derived_gaps
from nox.capabilities.model import Capability, CapabilityReport, CapabilityState, Gap
from nox.core.logging import get_logger
from nox.security.model import PermissionRequest, PermissionResult, Profile
from nox.tools.executor import split_tool_action
from nox.tools.registry import ToolRegistry

log = get_logger(__name__)

__all__ = ["DEFAULT_AGENT", "StateProbe", "build_report", "stale_gaps"]

#: Who the catalogue is compiled for. The model asks as itself, and its own agent name is what
#: makes the answer match what it would actually get when it calls.
DEFAULT_AGENT = "companion"

#: `None` means "nothing to report, the thing behind the tool is reachable". Anything else is the
#: state plus a sentence the user can act on ("no token stored for Home Assistant").
StateProbe = Callable[[str], tuple[CapabilityState, str] | None]


class PreviewEngine(Protocol):
    """The two things this module needs from the permission engine (so a test needs no real one)."""

    def preview(self, request: PermissionRequest) -> PermissionResult: ...
    def active_profile(self) -> Profile: ...


def _target_dependent(profile: Profile, tool: str) -> bool:
    """Does any rule that could match this tool narrow by target?

    `rule.tool` is a glob, so the tool name is matched against it and not the other way round - a
    rule written for `vault*` has to be found when asking about `vault.read`.
    """
    return any(rule.target != "*" and fnmatch(tool, rule.tool) for rule in profile.rules)


def stale_gaps(tools: ToolRegistry) -> list[str]:
    """Curated gaps that name a tool which now exists.

    The failure this prevents is the quiet one: a capability ships, the row stays behind, and Nox
    starts denying something it can do. Checked in the tests and again at build time, because a
    wrong "I cannot" is as damaging as a wrong "I can".
    """
    return [gap.name for gap in CURATED_GAPS if gap.name in tools]


def build_report(
    *,
    tools: ToolRegistry,
    engine: PreviewEngine,
    mode: str,
    privacy_mode: str,
    agent: str = DEFAULT_AGENT,
    sources: Mapping[str, str] | None = None,
    probe: StateProbe | None = None,
    installed_plugins: Iterable[str] = (),
    enabled_plugins: Iterable[str] = (),
    missing_prerequisites: Iterable[tuple[str, str]] = (),
) -> CapabilityReport:
    """One `CapabilityReport` for the state the system is in at this moment.

    `sources` maps tool name to the plugin that registered it; anything absent counts as core.
    `probe` answers the "does it actually work" question per tool - the registry cannot know.
    """
    profile = engine.active_profile()
    by_plugin = sources or {}
    capabilities: list[Capability] = []

    for described in tools.describe():
        tool, action = split_tool_action(described.name)
        decided = engine.preview(
            PermissionRequest(
                agent=agent,
                tool=tool,
                action=action,
                mode=mode,
                risk=described.risk,
                origin="local",
            )
        )
        state, reason = CapabilityState.AVAILABLE, ""
        probed = probe(described.name) if probe is not None else None
        if probed is not None:
            state, reason = probed
        capabilities.append(
            Capability(
                name=described.name,
                description=described.description,
                risk=described.risk,
                decision=decided.decision,
                rule=decided.rule_id,
                state=state,
                reason=reason,
                source=by_plugin.get(described.name, "core"),
                side_effects=described.side_effects,
                local=described.local,
                target_dependent=_target_dependent(profile, tool),
            )
        )

    stale = set(stale_gaps(tools))
    if stale:
        log.warning("capabilities.stale_gaps", gaps=sorted(stale))
    gaps: list[Gap] = [gap for gap in CURATED_GAPS if gap.name not in stale]
    gaps += derived_gaps(
        installed_plugins=installed_plugins,
        enabled_plugins=enabled_plugins,
        missing_prerequisites=missing_prerequisites,
    )

    report = CapabilityReport(
        mode=mode,
        profile=profile.id,
        privacy_mode=privacy_mode,
        capabilities=capabilities,
        gaps=gaps,
    )
    log.debug(
        "capabilities.report_built",
        mode=mode,
        profile=profile.id,
        tools=len(capabilities),
        usable=len(report.usable),
        gaps=len(gaps),
    )
    return report
