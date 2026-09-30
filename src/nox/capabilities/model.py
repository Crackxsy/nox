"""What Nox can do, what it may do, and what it cannot do - as data.

Three ideas are kept apart on purpose, because collapsing them is how an assistant ends up
claiming things that are not true:

* **Exists** - a tool is registered. Nothing more.
* **Permitted** - the active profile, mode and privacy state would allow the call. A tool can
  exist and be forbidden; that is a sentence Nox should be able to say.
* **Works** - the thing behind the tool is actually reachable. A smart-home tool exists and is
  permitted and still does nothing when no token is stored, and the honest answer names that.

A `Gap` is the fourth case and the one most systems leave out: something the user may reasonably
ask for that does not exist at all. Without it, "can you delete a file?" has no answer but silence.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from nox.security.model import Decision, Risk

__all__ = [
    "Capability",
    "CapabilityReport",
    "CapabilityState",
    "Gap",
    "GapReason",
]


class CapabilityState(StrEnum):
    """Whether the thing behind the tool is reachable right now."""

    AVAILABLE = "available"
    #: Reachable, but not fully: a degraded backend, a missing optional package.
    LIMITED = "limited"
    #: Not reachable: the plugin is not running, a credential is missing, a probe fails.
    UNAVAILABLE = "unavailable"


class GapReason(StrEnum):
    """Why a capability the user might ask for is absent."""

    #: Not built. The honest default for anything still on the roadmap.
    MISSING = "missing"
    #: Built, but switched off here (a plugin absent from `plugins.enabled`).
    DISABLED = "disabled"
    #: Built and enabled, but something the user must supply is not there (a token, a device).
    PREREQUISITE = "prerequisite"
    #: Built deliberately without it. A boundary, not a backlog item.
    FORBIDDEN = "forbidden"


class Capability(BaseModel):
    """One tool, with the two questions a user actually asks about it."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    risk: Risk
    #: What the permission engine would decide right now, asked without recording it.
    decision: Decision
    #: Which rule decided, so a surprising answer can be traced to the profile that caused it.
    rule: str
    state: CapabilityState = CapabilityState.AVAILABLE
    #: Empty while available; otherwise what is wrong, in words the user can act on.
    reason: str = ""
    #: `core`, or the id of the plugin that registered the tool.
    source: str = "core"
    side_effects: bool = True
    local: bool = True
    #: The active profile decides this tool differently depending on what it is pointed at, so the
    #: decision above is the one for a call with no particular target. Set rather than hidden,
    #: because "you may read the vault but not your Documents" is exactly the kind of answer that
    #: must not be flattened into a plain yes.
    target_dependent: bool = False

    @property
    def usable(self) -> bool:
        """Permitted without asking *and* reachable.

        Deliberately strict: `confirm` is not usable without the user standing there, and keeping
        that distinct from `allow` is the whole point of the field.
        """
        return self.decision is Decision.ALLOW and self.state is CapabilityState.AVAILABLE


class Gap(BaseModel):
    """Something a user may reasonably ask for that Nox does not have."""

    model_config = ConfigDict(frozen=True)

    #: A short handle, in the shape a tool would have if it existed (`file.delete`).
    name: str
    #: What it would do, in one sentence.
    what: str
    reason: GapReason
    #: The specific explanation: which package, which setting, which boundary.
    detail: str = ""


class CapabilityReport(BaseModel):
    """The whole answer to "what can you do?", as one object.

    Carries the mode, profile and privacy state it was taken under, because every decision in it
    depends on those three and a report without them cannot be read correctly an hour later.
    """

    model_config = ConfigDict(frozen=True)

    mode: str
    profile: str
    privacy_mode: str
    capabilities: list[Capability] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)

    @property
    def usable(self) -> list[Capability]:
        return [capability for capability in self.capabilities if capability.usable]

    def find(self, name: str) -> Capability | None:
        return next((c for c in self.capabilities if c.name == name), None)
