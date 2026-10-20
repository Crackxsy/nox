"""Exact boundaries of the permission engine that a mutation run showed unprotected:
grant expiry at the very instant, CRITICAL never grantable, privacy-zone redaction in the audit.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from nox.security.audit import SqliteAuditLog
from nox.security.model import Profile, Risk, TemporaryGrant
from nox.security.permissions import DefaultPermissionEngine, InMemoryGrantStore, grant_matches
from nox.security.profiles import InMemoryProfileProvider
from tests.unit.fakes import FakeBus, MutableClock
from tests.unit.tools.conftest import FakePrivacy

from .conftest import req


def _grant(clock: MutableClock, *, expires_in_s: float, max_risk: Risk) -> TemporaryGrant:
    return TemporaryGrant(
        grant_id="g",
        scope="session:*/*",
        max_risk=max_risk,
        expires_at=clock.now + timedelta(seconds=expires_in_s),
    )


def test_a_grant_is_dead_at_the_instant_it_expires() -> None:
    clock = MutableClock()
    request = req("file", "write", Risk.MEDIUM)

    alive = _grant(clock, expires_in_s=0.000001, max_risk=Risk.HIGH)
    at_expiry = _grant(clock, expires_in_s=0, max_risk=Risk.HIGH)

    assert grant_matches(alive, request, clock.now) is True
    assert grant_matches(at_expiry, request, clock.now) is False


def test_no_grant_ever_covers_a_critical_request() -> None:
    clock = MutableClock()
    grant = _grant(clock, expires_in_s=3600, max_risk=Risk.CRITICAL)

    assert grant_matches(grant, req("system", "wipe", Risk.CRITICAL), clock.now) is False
    assert grant_matches(grant, req("file", "write", Risk.HIGH), clock.now) is True


def _engine(privacy: FakePrivacy) -> tuple[DefaultPermissionEngine, SqliteAuditLog]:
    bus = FakeBus()
    clock = MutableClock()
    audit = SqliteAuditLog(sqlite3.connect(":memory:"), bus=bus, clock=clock)
    profile = Profile(
        id="t", description="t", rules=[], cloud_allowed=True, memory_writes_allowed=True
    )
    engine = DefaultPermissionEngine(
        profiles=InMemoryProfileProvider([profile]),
        privacy=privacy,
        grants=InMemoryGrantStore(),
        audit=audit,
        bus=bus,
        clock=clock,
        initial_profile="t",
    )
    return engine, audit


def test_a_target_is_redacted_in_the_audit_while_a_privacy_zone_is_active() -> None:
    privacy = FakePrivacy()
    engine, audit = _engine(privacy)

    engine.check(req("file", "read", Risk.READ, target="diary/today.md"))
    privacy.zone_active = True
    engine.check(req("file", "read", Risk.READ, target="diary/today.md"))

    targets = [entry.target for entry in audit.entries()]
    assert targets == ["diary/today.md", "<redacted>"]
