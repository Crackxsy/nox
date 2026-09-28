"""Privacy mode, panic and the kill switch survive a restart.

The restart is simulated the way it happens: the security core is built, changed, closed, and
built again on the same database file. Nothing may come back less private or less stopped than it
went down, and a stored state that cannot be read boots with the strictest mode, in safe mode.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from nox.core.config import NoxConfig
from nox.core.events import E
from nox.core.state import PrivacyMode
from nox.data.db import Database
from nox.data.security_repos import SecurityStateRepository
from nox.security.model import Decision, Risk
from nox.security.persisted_state import (
    InMemorySecurityStateStore,
    PersistedSecurityState,
    SecurityStateUnreadableError,
)
from nox.security.secrets import InMemorySecretStore
from nox.security.service import SecurityContext
from tests.unit.fakes import FakeBus

from .conftest import DEFAULTS_YAML, PROFILES_DIR, req


@pytest.fixture
def config() -> NoxConfig:
    return NoxConfig.model_validate(yaml.safe_load(DEFAULTS_YAML.read_text(encoding="utf-8")))


class Boots:
    """Builds security cores on one database file, and closes every one of them afterwards."""

    def __init__(self, path: Path, config: NoxConfig) -> None:
        self.path = path
        self.config = config
        self._open: list[tuple[SecurityContext, Database]] = []

    def boot(self, *, bus: FakeBus | None = None) -> SecurityContext:
        db = Database(self.path)
        db.migrate()
        ctx = SecurityContext.build(
            self.config,
            conn=db.connection,
            db_lock=db.lock,
            profiles_dir=PROFILES_DIR,
            bus=bus,
            secret_store=InMemorySecretStore(),
            state_store=SecurityStateRepository(db),
        )
        self._open.append((ctx, db))
        return ctx

    def shut_down(self, ctx: SecurityContext) -> None:
        for index, (candidate, db) in enumerate(self._open):
            if candidate is ctx:
                assert ctx.close()
                db.close()
                del self._open[index]
                return

    def close_all(self) -> None:
        for ctx, db in self._open:
            ctx.close()
            db.close()
        self._open.clear()


@pytest.fixture
def boots(tmp_path: Path, config: NoxConfig) -> Iterator[Boots]:
    manager = Boots(tmp_path / "nox.db", config)
    yield manager
    manager.close_all()


async def test_a_fresh_install_boots_with_the_configured_mode(boots: Boots) -> None:
    ctx = boots.boot()
    assert ctx.privacy.mode is PrivacyMode.BALANCED
    assert not ctx.killswitch.is_engaged()
    assert ctx.restored is not None and ctx.restored.source == "configured"


async def test_privacy_mode_set_at_runtime_survives_a_restart(boots: Boots) -> None:
    first = boots.boot()
    await first.privacy.set_mode(PrivacyMode.OFFLINE, by="tray")
    boots.shut_down(first)

    second = boots.boot()

    assert second.privacy.mode is PrivacyMode.OFFLINE
    assert not second.egress.check("api.anthropic.com", 443).allowed


async def test_an_engaged_kill_switch_survives_a_restart_and_needs_the_normal_resume(
    boots: Boots,
) -> None:
    first = boots.boot()
    await first.killswitch.engage("audit", "chain broken")  # a security-path kill
    boots.shut_down(first)

    second = boots.boot()

    assert second.killswitch.is_engaged()
    assert second.killswitch.security_path is True
    assert second.privacy.snapshot().safe_mode
    assert second.engine.check(req("pet", "express", Risk.LOW)).rule_id == "safe_mode"
    assert await second.killswitch.resume(pin_ok=False) is False  # the PIN is still required
    assert await second.killswitch.resume(pin_ok=True) is True
    boots.shut_down(second)

    third = boots.boot()
    assert not third.killswitch.is_engaged()
    assert third.engine.check(req("pet", "express", Risk.LOW)).decision is Decision.ALLOW


async def test_panic_survives_a_restart(boots: Boots) -> None:
    first = boots.boot()
    await first.killswitch.panic(by="dashboard")
    boots.shut_down(first)

    second = boots.boot()

    assert second.privacy.mode is PrivacyMode.OFFLINE
    assert second.privacy.panic is True
    assert second.killswitch.is_engaged() and second.killswitch.security_path


async def test_an_unreadable_state_boots_strictest_and_in_safe_mode(
    boots: Boots, tmp_path: Path
) -> None:
    first = boots.boot()
    await first.privacy.set_mode(PrivacyMode.PRIVATE, by="tray")
    boots.shut_down(first)
    damaged = Database(tmp_path / "nox.db")
    damaged.execute("UPDATE security_state SET kill_engaged = 9 WHERE id = 1")
    damaged.close()

    second = boots.boot()

    # PRIVATE (stored) is stricter than BALANCED (configured): the stored mode wins.
    assert second.privacy.mode is PrivacyMode.PRIVATE
    assert second.killswitch.is_engaged() and second.killswitch.security_path
    assert second.restored is not None and second.restored.source == "unreadable"


async def test_an_unreadable_state_never_relaxes_a_stricter_configuration(
    config: NoxConfig,
) -> None:
    class Unreadable:
        def load(self) -> PersistedSecurityState | None:
            raise SecurityStateUnreadableError("torn row", privacy_mode=PrivacyMode.FULL)

        def save(self, state: PersistedSecurityState) -> None:
            raise AssertionError("not reached in this test")

    config.privacy.mode = "offline"
    ctx = SecurityContext.build(
        config,
        conn=Database(":memory:").connection,
        profiles_dir=PROFILES_DIR,
        secret_store=InMemorySecretStore(),
        state_store=Unreadable(),
    )
    try:
        assert ctx.privacy.mode is PrivacyMode.OFFLINE  # FULL stored, OFFLINE configured
        assert ctx.killswitch.is_engaged()
    finally:
        ctx.close()


async def test_a_stricter_mode_is_written_before_it_is_announced(config: NoxConfig) -> None:
    store = InMemorySecurityStateStore()
    bus = FakeBus()
    seen_at_announcement: list[PrivacyMode | None] = []

    async def on_mode(_event: object) -> None:
        seen_at_announcement.append(store.state.privacy_mode if store.state else None)

    bus.subscribe(E.PRIVACY_MODE_CHANGED, on_mode)
    ctx = SecurityContext.build(
        config,
        conn=Database(":memory:").connection,
        profiles_dir=PROFILES_DIR,
        bus=bus,
        secret_store=InMemorySecretStore(),
        state_store=store,
    )
    try:
        await ctx.privacy.set_mode(PrivacyMode.OFFLINE, by="tray")
        assert seen_at_announcement == [PrivacyMode.OFFLINE]
        await ctx.killswitch.engage("ui", "stop")
        assert store.state is not None and store.state.kill_engaged
        assert store.state.kill_origin == "ui" and not store.state.kill_security_path
    finally:
        ctx.close()


async def test_a_failed_write_keeps_the_stricter_state_in_memory(config: NoxConfig) -> None:
    class Failing(InMemorySecurityStateStore):
        def save(self, state: PersistedSecurityState) -> None:
            raise OSError("disk full")

    ctx = SecurityContext.build(
        config,
        conn=Database(":memory:").connection,
        profiles_dir=PROFILES_DIR,
        secret_store=InMemorySecretStore(),
        state_store=Failing(),
    )
    try:
        await ctx.privacy.set_mode(PrivacyMode.OFFLINE, by="tray")
        await ctx.killswitch.engage("ui", "stop")
        assert ctx.privacy.mode is PrivacyMode.OFFLINE
        assert ctx.killswitch.is_engaged()
        assert ctx.state_recorder is not None and ctx.state_recorder.failures >= 1
    finally:
        ctx.close()
