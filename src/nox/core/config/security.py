"""Security and privacy sections.

The profile ids are not written out here: they come from `nox.security.constants`, so the
`Literal` the configuration validates against, the profile loader and the shipped
`config/profiles/*.yaml` can never drift apart.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from nox.core.config.types import StrictSection
from nox.core.netloc import NetlocError, split_netloc
from nox.security.constants import (
    DEFAULT_LOOPBACK_ALLOWLIST,
    SECURITY_PROFILE_IDS,
    SecurityProfileId,
)
from nox.security.hardlist import HARD_PROHIBITIONS

__all__ = [
    "DEFAULT_LOOPBACK_ALLOWLIST",
    "SECURITY_PROFILE_IDS",
    "AuditConfig",
    "CaptureConfig",
    "PrivacyConfig",
    "RetentionConfig",
    "SecurityConfig",
    "SecurityProfileId",
]


class AuditConfig(StrictSection):
    enabled: bool = True
    retention_days_security: int = Field(default=730, ge=1)
    retention_days_normal: int = Field(default=14, ge=1)


def _validated_allowlist(entries: list[str]) -> list[str]:
    """Reject an unparsable allow-list entry while the file is being read.

    An entry the matcher cannot parse would otherwise sit in the configuration looking effective
    and match nothing. The failure mode is a service that is silently unreachable, which is very
    hard to tell apart from the service being down.
    """
    for entry in entries:
        if entry.strip().lower() in ("", "none"):
            continue
        try:
            split_netloc(entry)
        except NetlocError as exc:
            raise ValueError(str(exc)) from exc
    return entries


class SecurityConfig(StrictSection):
    profile: SecurityProfileId = "companion"
    #: When a PIN is configured, require it for a change that *relaxes* the security posture:
    #: leaving a stricter privacy mode, re-enabling a capture device, and editing a `security.*`
    #: or `privacy.*` setting from the dashboard. Tightening never asks for the PIN.
    pin_required_for_security_changes: bool = True
    hard_prohibitions: list[str] = Field(default_factory=lambda: sorted(HARD_PROHIBITIONS))
    #: Global network allow-list (`host`, `host:port`, `*.example.com:443`, `[::1]:443`). A
    #: profile with its own `egress_allowlist`, or with `cloud_allowed: false`, uses that instead.
    egress_allowlist: list[str] = Field(default_factory=list)
    #: Loopback services that stay reachable in privacy mode private or offline. A profile adds
    #: entries to this list rather than replacing it.
    loopback_allowlist: list[str] = Field(default_factory=lambda: list(DEFAULT_LOOPBACK_ALLOWLIST))
    audit: AuditConfig = Field(default_factory=AuditConfig)

    @field_validator("hard_prohibitions")
    @classmethod
    def _must_contain_hardlist(cls, value: list[str]) -> list[str]:
        missing = HARD_PROHIBITIONS.difference(value)
        if missing:
            raise ValueError(
                "hard_prohibitions may only add entries; missing constant entries: "
                + ", ".join(sorted(missing))
            )
        return sorted(set(value))  # stable order, no duplicates

    @field_validator("egress_allowlist", "loopback_allowlist")
    @classmethod
    def _entries_parse(cls, value: list[str]) -> list[str]:
        return _validated_allowlist(value)


class CaptureConfig(StrictSection):
    microphone: bool = True
    camera: bool = False
    screen: bool = True


class RetentionConfig(StrictSection):
    raw_transcripts_days: int = Field(default=7, ge=0)
    logs_days: int = Field(default=14, ge=1)
    metrics_days: int = Field(default=365, ge=1)
    viewer_data_inactive_months: int = Field(default=12, ge=1)


class PrivacyConfig(StrictSection):
    mode: Literal["full", "balanced", "private", "offline"] = "balanced"
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    zones: list[str] = Field(default_factory=list)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
