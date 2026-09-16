"""SecurityContext: builds from defaults.yaml, boot verification, kill switch wired into the
engine."""

from __future__ import annotations

import sqlite3

import pytest
import yaml

from nox.core.state import PrivacyMode
from nox.security.model import Decision, Risk
from nox.security.prohibitions import HardProhibitionRemovedError
from nox.security.secrets import InMemorySecretStore
from nox.security.service import SecurityContext
from tests.unit.fakes import FakeBus

from .conftest import DEFAULTS_YAML, PROFILES_DIR, MutableClock, req


@pytest.fixture
def config() -> dict:  # type: ignore[type-arg]
    data: dict = yaml.safe_load(DEFAULTS_YAML.read_text(encoding="utf-8"))  # type: ignore[type-arg]
    return data


async def test_build_and_boot(
    config: dict,
    conn: sqlite3.Connection,
    bus: FakeBus,  # type: ignore[type-arg]
    clock: MutableClock,
) -> None:
    ctx = SecurityContext.build(
        config,
        conn=conn,
        profiles_dir=PROFILES_DIR,
        bus=bus,
        secret_store=InMemorySecretStore(),
        clock=clock,
        session_id="boot",
    )
    assert ctx.engine.active_profile().id == "companion"
    assert ctx.privacy.mode is PrivacyMode.BALANCED and len(ctx.privacy.zones) == 6
    assert len(ctx.hard_prohibitions) == 9
    assert ctx.verify_boot().ok
    assert ctx.engine.check(req("pet", "express", Risk.LOW)).decision is Decision.ALLOW
    await ctx.killswitch.engage("supervisor", "hang")
    assert ctx.privacy.snapshot().safe_mode
    assert ctx.engine.check(req("pet", "express", Risk.LOW)).rule_id == "safe_mode"
    assert not ctx.privacy.allows_cloud() and not ctx.egress.check("api.anthropic.com", 443).allowed
    assert await ctx.killswitch.resume(pin_ok=True)
    assert ctx.engine.check(req("pet", "express", Risk.LOW)).decision is Decision.ALLOW
    assert ctx.audit.verify_chain()


async def test_build_wires_both_egress_allowlists_from_the_config(
    config: dict,  # type: ignore[type-arg]
    conn: sqlite3.Connection,
) -> None:
    """B-11/OP-7 C: `security.egress_allowlist` and `security.loopback_allowlist` reach the
    guard."""
    config["security"]["egress_allowlist"] = ["api.example.com:443"]
    # The global list only applies to a profile that inherits it; since EPIC-17 `companion` has its
    # own entry (api.telegram.org:443), so this test switches to `coding`, which still inherits.
    config["security"]["profile"] = "coding"
    ctx = SecurityContext.build(
        config, conn=conn, profiles_dir=PROFILES_DIR, secret_store=InMemorySecretStore()
    )
    assert config["security"]["loopback_allowlist"] == ["127.0.0.1:11434"]  # from defaults.yaml
    assert ctx.egress.check("api.example.com", 443).allowed  # BALANCED: the global list applies
    assert not ctx.egress.check("api.anthropic.com", 443).allowed
    await ctx.privacy.set_mode(PrivacyMode.OFFLINE)
    assert ctx.egress.check("127.0.0.1", 11434).allowed
    assert not ctx.egress.check("127.0.0.1", 47800).allowed
    assert not ctx.egress.check("api.example.com", 443).allowed


def test_build_rejects_config_that_drops_a_hard_prohibition(
    config: dict, conn: sqlite3.Connection
) -> None:  # type: ignore[type-arg]
    config["security"]["hard_prohibitions"].remove("stream.stop")
    with pytest.raises(HardProhibitionRemovedError):
        SecurityContext.build(
            config, conn=conn, profiles_dir=PROFILES_DIR, secret_store=InMemorySecretStore()
        )


def test_verify_boot_reports_broken_chain(config: dict, conn: sqlite3.Connection) -> None:  # type: ignore[type-arg]
    ctx = SecurityContext.build(
        config, conn=conn, profiles_dir=PROFILES_DIR, secret_store=InMemorySecretStore()
    )
    ctx.audit.append(actor="a", tool="t", action="x", target="", decision="allow", result="ok")
    conn.execute("DROP TRIGGER audit_log_no_update")
    conn.execute("UPDATE audit_log SET actor = 'evil' WHERE seq = 1")
    conn.commit()
    v = ctx.verify_boot()
    assert not v.ok and v.first_bad_seq == 1
    assert ctx.audit.entries()[-1].action == "audit.verify"
