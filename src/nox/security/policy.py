"""`EffectivePolicy`: the one place where the active profile and the privacy state are combined.

A profile says what a kind of work may do at all (`cloud_allowed`, `memory_writes_allowed`,
`integrations_allowed`); the privacy service says what the moment allows (the mode, a privacy
zone, panic, the kill switch). Every decision that is not a tool call - which language model
answers, whether a conversation turn or a memory item is stored, whether an integration may start
- needs both, and used to get only the privacy half: the `work` profile's "no cloud" and "nothing
remembered" were never consulted outside the permission engine.

So each such consumer asks this object and nothing else: the router and the Claude Code provider
(`provider_block_reason`, `cloud_block_reason`), the orchestrator's turn store, the memory service
and the vault writer (`allows_memory_write`), and the plugin manager
(`plugin_start_block_reason`). Nobody re-derives an answer from `Profile` fields or the privacy
mode on their own. Both inputs are read at call time, so a profile switch (`mode.set`, the Rocket
League bridge) or a privacy change applies to the very next question - no restart.

Tool calls keep going through the permission engine, whose guards apply the same fields per
request; this module does not replace them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from nox.core.state import PrivacyMode
from nox.security.model import Profile

__all__ = ["EffectivePolicy", "PolicyPrivacy"]

#: Privacy modes in which nothing leaves the machine.
_LOCAL_ONLY_MODES = frozenset({PrivacyMode.PRIVATE, PrivacyMode.OFFLINE})


class PolicyPrivacy(Protocol):
    """The part of `PrivacyService` the policy reads."""

    @property
    def mode(self) -> PrivacyMode: ...
    def allows_cloud(self) -> bool: ...
    def allows_memory_write(self) -> bool: ...


class EffectivePolicy:
    """Profile x privacy, answered as a reason: empty means allowed, text says why not."""

    def __init__(self, *, privacy: PolicyPrivacy, profile: Callable[[], Profile]) -> None:
        self._privacy = privacy
        self._profile = profile

    # ---- cloud -----------------------------------------------------------------------------------

    def cloud_block_reason(self) -> str:
        """Why nothing may go to a cloud service right now; empty when it may."""
        if not self._privacy.allows_cloud():
            mode = self._privacy.mode
            if mode in _LOCAL_ONLY_MODES:
                return f"cloud blocked by privacy mode {mode.value}"
            return "cloud blocked while the kill switch or panic is engaged"
        profile = self._profile()
        if not profile.cloud_allowed:
            return f"cloud blocked by profile {profile.id}"
        return ""

    def cloud_allowed(self) -> bool:
        return not self.cloud_block_reason()

    # ---- memory ----------------------------------------------------------------------------------

    def memory_write_block_reason(self) -> str:
        """Why nothing may be written to memory, the vault or the turn history right now."""
        profile = self._profile()
        if not profile.memory_writes_allowed:
            return f"memory writes blocked by profile {profile.id}"
        if not self._privacy.allows_memory_write():
            return "memory writes blocked by the privacy mode, a privacy zone or safe mode"
        return ""

    def allows_memory_write(self) -> bool:
        """The `MemoryPolicy`/`PrivacyGate` protocol the orchestrator and memory service use."""
        return not self.memory_write_block_reason()

    # ---- integrations ----------------------------------------------------------------------------

    def integration_block_reason(self, integration: str) -> str:
        """Whether the active profile names `integration` in `integrations_allowed`.

        An empty list restricts nothing (like `tools_allowed`), and an empty `integration` is a
        part of Nox itself, never an integration.
        """
        profile = self._profile()
        allowed = profile.integrations_allowed
        if not integration or not allowed or integration in allowed:
            return ""
        return f"profile {profile.id} does not allow the {integration} integration"

    def provider_block_reason(self, provider_id: str, *, cloud: bool) -> str:
        """For a language-model provider: the cloud rule if it is remote, then its integration."""
        if cloud:
            reason = self.cloud_block_reason()
            if reason:
                return reason
        return self.integration_block_reason(provider_id)

    def plugin_start_block_reason(self, integration: str, *, network: bool, cloud: bool) -> str:
        """Whether a plugin may run now.

        `network`: it talks to a host beyond this machine (a non-loopback `network.egress` entry),
        which no privacy mode but full and balanced permits. `cloud`: it hands data to a cloud
        service through a process of its own that the egress guard cannot see (the Claude Code
        CLI), so the cloud rule applies to it as a whole.
        """
        reason = self.integration_block_reason(integration)
        if reason:
            return reason
        if cloud:
            reason = self.cloud_block_reason()
            if reason:
                return reason
        mode = self._privacy.mode
        if network and mode in _LOCAL_ONLY_MODES:
            return f"privacy mode {mode.value} keeps integrations off the network"
        return ""
