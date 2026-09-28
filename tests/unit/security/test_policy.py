"""`EffectivePolicy`: profile and privacy combined, read live, and wired into every consumer that is
not a tool call - the composition root included."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

import nox.app as app
from nox.ai.base import AiRequest, AiRole, Message
from nox.core.config import NoxConfig
from nox.core.orchestrator import Orchestrator
from nox.core.state import PrivacyMode
from nox.data.db import Database
from nox.memory.vault_writer import VaultWriter, VaultWriteRefusedError
from nox.security.model import Profile
from nox.security.policy import EffectivePolicy
from nox.security.privacy import UNOBSERVABLE_ZONE, PrivacyService
from nox.security.secrets import InMemorySecretStore
from nox.security.service import SecurityContext
from tests.unit.fakes import FakeBus, FakeRouter, FakeState, FakeTurns

from .conftest import DEFAULTS_YAML, PROFILES_DIR


def _profile(**fields: Any) -> Profile:
    return Profile.model_validate({"id": "custom", "rules": [], **fields})


def _policy(
    privacy: PrivacyService, profile: Profile
) -> tuple[EffectivePolicy, dict[str, Profile]]:
    holder = {"profile": profile}
    return EffectivePolicy(privacy=privacy, profile=lambda: holder["profile"]), holder


# ---- the answers -------------------------------------------------------------------------------


async def test_cloud_needs_both_the_profile_and_the_privacy_mode() -> None:
    privacy = PrivacyService(mode=PrivacyMode.BALANCED)
    policy, holder = _policy(privacy, _profile(cloud_allowed=True))
    assert policy.cloud_allowed() and policy.cloud_block_reason() == ""

    holder["profile"] = _profile(id="work", cloud_allowed=False)
    assert policy.cloud_block_reason() == "cloud blocked by profile work"

    holder["profile"] = _profile(cloud_allowed=True)
    await privacy.set_mode(PrivacyMode.PRIVATE)
    assert policy.cloud_block_reason() == "cloud blocked by privacy mode private"


async def test_cloud_is_blocked_while_the_kill_switch_is_engaged() -> None:
    engaged = {"on": True}
    privacy = PrivacyService(mode=PrivacyMode.FULL, safe_mode=lambda: engaged["on"])
    policy, _ = _policy(privacy, _profile(cloud_allowed=True))
    assert "kill switch" in policy.cloud_block_reason()
    engaged["on"] = False
    assert policy.cloud_allowed()


async def test_memory_writes_need_both_the_profile_and_the_privacy_state() -> None:
    privacy = PrivacyService(zones=["banking"])
    policy, holder = _policy(privacy, _profile(id="work", memory_writes_allowed=False))
    assert policy.memory_write_block_reason() == "memory writes blocked by profile work"

    holder["profile"] = _profile(memory_writes_allowed=True)
    assert policy.allows_memory_write()
    await privacy.observe_foreground("Sparkasse Online-Banking", "browser.exe")
    assert not policy.allows_memory_write()


async def test_the_unobservable_zone_follows_its_policy_for_memory() -> None:
    screen_only = PrivacyService(unobservable_policy="screen_only")
    strict = PrivacyService(unobservable_policy="strict")
    for privacy in (screen_only, strict):
        assert await privacy.observe_foreground_unobservable() == UNOBSERVABLE_ZONE
    assert _policy(screen_only, _profile())[0].allows_memory_write()
    assert not _policy(strict, _profile())[0].allows_memory_write()


def test_integrations_are_gated_by_the_profile_list_only_when_it_names_any() -> None:
    privacy = PrivacyService()
    policy, holder = _policy(privacy, _profile(id="work", integrations_allowed=["ollama"]))
    assert policy.integration_block_reason("ollama") == ""
    assert policy.integration_block_reason("") == ""  # part of Nox, not an integration
    assert "does not allow the telegram integration" in policy.integration_block_reason("telegram")
    holder["profile"] = _profile(integrations_allowed=[])
    assert policy.integration_block_reason("telegram") == ""


async def test_plugin_starts_follow_integration_cloud_and_network_rules() -> None:
    privacy = PrivacyService(mode=PrivacyMode.BALANCED)
    profile = _profile(id="coding", integrations_allowed=["claude_code", "telegram"])
    policy, _ = _policy(privacy, profile)
    assert policy.plugin_start_block_reason("claude_code", network=False, cloud=True) == ""
    assert policy.plugin_start_block_reason("telegram", network=True, cloud=False) == ""
    blocked = policy.plugin_start_block_reason("twitch", network=True, cloud=False)
    assert "does not allow the twitch integration" in blocked

    await privacy.set_mode(PrivacyMode.OFFLINE)
    assert "offline" in policy.plugin_start_block_reason("telegram", network=True, cloud=False)
    assert "offline" in policy.plugin_start_block_reason("claude_code", network=False, cloud=True)
    assert policy.plugin_start_block_reason("", network=False, cloud=False) == ""


# ---- the consumers -----------------------------------------------------------------------------


async def test_the_orchestrator_records_no_turn_in_a_profile_that_remembers_nothing() -> None:
    privacy = PrivacyService(mode=PrivacyMode.BALANCED)
    policy, holder = _policy(privacy, _profile(id="work", memory_writes_allowed=False))
    bus = FakeBus()
    turns = FakeTurns()
    orchestrator = Orchestrator(
        bus=bus,
        state=FakeState(),
        router=FakeRouter(bus),
        speaker=None,
        turns=turns,
        memory_policy=policy,
        system_prompt=lambda: "You are Nox.",
    )
    await orchestrator.handle_text("Wie spät ist es?", speak=False)
    assert turns.rows == []

    holder["profile"] = _profile(memory_writes_allowed=True)  # a profile switch, no restart
    await orchestrator.handle_text("Wie spät ist es?", speak=False)
    assert len(turns.rows) == 2


def test_the_vault_writer_refuses_while_the_policy_forbids_memory_writes(tmp_path: Path) -> None:
    database = Database(tmp_path / "nox.db")
    database.migrate()
    try:
        privacy = PrivacyService(mode=PrivacyMode.BALANCED)
        policy, holder = _policy(privacy, _profile(id="work", memory_writes_allowed=False))
        writer = VaultWriter(database, tmp_path / "vault", gate=policy)
        with pytest.raises(VaultWriteRefusedError, match="forbids memory writes"):
            writer.write_inbox_note("Notiz", "Inhalt", source="test")
        assert not (tmp_path / "vault" / "00 - Inbox").exists()

        holder["profile"] = _profile(memory_writes_allowed=True)
        assert writer.write_inbox_note("Notiz", "Inhalt", source="test").action == "created"
    finally:
        database.close()


# ---- the composition root ----------------------------------------------------------------------


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    yield connection
    connection.close()


@pytest.fixture
def work_core(conn: sqlite3.Connection) -> Iterator[app.NoxCore]:
    """A core with the security layer and the models built exactly as `start()` builds them,
    in the `work` profile and the default BALANCED privacy."""
    data = yaml.safe_load(DEFAULTS_YAML.read_text(encoding="utf-8"))
    data["security"]["profile"] = "work"
    # No real model server and no real CLI: the test is about which provider may be asked.
    data["ai"]["providers"]["ollama"]["enabled"] = False
    data["ai"]["providers"]["claude_code"]["command"] = "definitely-not-a-real-claude-cli"
    config = NoxConfig.model_validate(data)
    core = app.NoxCore(config, voice=False, extensions=False)
    core.bus = FakeBus()  # type: ignore[assignment]
    core.security = SecurityContext.build(
        config,
        conn=conn,
        profiles_dir=PROFILES_DIR,
        bus=core.bus,
        secret_store=InMemorySecretStore(),
    )
    core._build_models()
    yield core
    assert core.security.close(), "audit writer did not drain"


def _chat(request_id: str) -> AiRequest:
    return AiRequest(
        request_id=request_id,
        role=AiRole.CHAT,
        messages=[Message(role="user", content="Hier ist Code aus der Firma, erklär ihn mir.")],
        privacy_mode="balanced",
        timeout_s=5.0,
    )


async def test_the_core_never_asks_the_cloud_in_the_work_profile(work_core: app.NoxCore) -> None:
    router = work_core.router
    assert router is not None
    response = await router.complete(_chat("work-1"))
    assert response.provider == "rules"
    assert "claude_code: skipped (cloud blocked by profile work)" in router.explain("work-1")


async def test_the_cores_router_follows_a_profile_switch_at_once(work_core: app.NoxCore) -> None:
    router, security = work_core.router, work_core.security
    assert router is not None and security is not None
    security.engine.set_profile("companion", by="test")
    await router.complete(_chat("companion-1"))
    explanation = router.explain("companion-1")
    # Asked now (and found missing), no longer blocked by the profile.
    assert "cloud blocked" not in explanation
    assert "claude_code: skipped (unavailable" in explanation
