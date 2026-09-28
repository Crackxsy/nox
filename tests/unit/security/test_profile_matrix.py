"""Every shipped profile x every privacy mode: does chat reach the cloud, does an escalated turn,
may memory be written, which plugins start?

The expected outcomes are written out by hand below, from what each profile file promises - they
are deliberately not computed by the code under test. The components are the real ones, wired the
way the core wires them: the shipped profiles, the permission engine, `PrivacyService`, the
effective policy, `build_router`, `MemoryService`, and the `PluginManager` over the shipped
`plugins/` manifests (only the processes and the hub are fakes).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
import yaml

from nox.ai.base import AiRequest, AiRole, Message
from nox.ai.config import AiConfig
from nox.ai.escalation import EscalationPolicy
from nox.core.boot.ai import build_router
from nox.core.config import NoxConfig
from nox.core.events import HealthStatus
from nox.core.state import PrivacyMode
from nox.ipc.dispatch import RequestRegistry
from nox.memory.items import MemoryService, MemoryWriteRefusedError
from nox.plugins.manager import PluginManager, PluginManagerSettings, PluginState
from nox.security.permissions import DefaultPermissionEngine
from nox.security.policy import EffectivePolicy
from nox.security.privacy import PrivacyService
from nox.security.profiles import YamlProfileProvider
from tests.unit.ai.conftest import FakeProvider
from tests.unit.fakes import FakeBus
from tests.unit.plugins.conftest import FakeHub, FakeProcess, FakeTokens

from .conftest import DEFAULTS_YAML, PROFILES_DIR, REPO

PLUGINS_DIR = REPO / "plugins"
SHIPPED_PLUGINS = sorted(p.parent.name for p in PLUGINS_DIR.glob("*/manifest.yaml"))
MODES = list(PrivacyMode)
LOCAL_ONLY = {PrivacyMode.PRIVATE, PrivacyMode.OFFLINE}


@dataclass(frozen=True)
class Promise:
    """What a profile file says, in the terms the matrix checks."""

    cloud: bool
    memory: bool
    #: Plugins that start in full/balanced privacy (subject to `plugins.enabled`).
    plugins: frozenset[str]


#: Plugins every profile runs: local-only, no integration of their own.
LOCAL_EVERYWHERE = frozenset({"creative", "echo", "rl"})
#: Plugins that reach a host beyond this machine, or the cloud through their own process: they
#: never run in private or offline privacy.
OFF_THE_MACHINE = frozenset({"coding", "home", "telegram", "twitch"})

PROMISES: dict[str, Promise] = {
    "companion": Promise(True, True, LOCAL_EVERYWHERE | {"clips", "home", "telegram"}),
    "coding": Promise(True, True, LOCAL_EVERYWHERE | {"clips", "coding"}),
    "offline": Promise(False, True, LOCAL_EVERYWHERE),
    "research": Promise(True, True, LOCAL_EVERYWHERE),
    "rocket_league": Promise(False, True, LOCAL_EVERYWHERE),
    "stream": Promise(True, True, LOCAL_EVERYWHERE | {"clips", "obs", "twitch"}),
    "work": Promise(False, False, LOCAL_EVERYWHERE),
}

CELLS = [(profile, mode) for profile in sorted(PROMISES) for mode in MODES]
CELL_IDS = [f"{profile}-{mode.value}" for profile, mode in CELLS]


def test_the_matrix_covers_every_shipped_profile_and_plugin() -> None:
    assert sorted(PROMISES) == YamlProfileProvider(PROFILES_DIR).available()
    covered = set().union(*(p.plugins for p in PROMISES.values()))
    assert covered == set(SHIPPED_PLUGINS)


# ---- wiring ------------------------------------------------------------------------------------


def _defaults() -> NoxConfig:
    return NoxConfig.model_validate(yaml.safe_load(DEFAULTS_YAML.read_text(encoding="utf-8")))


class _Setup:
    """One (profile, privacy mode) cell, built from the real components."""

    def __init__(self, profile_id: str, mode: PrivacyMode) -> None:
        self.bus = FakeBus()
        self.privacy = PrivacyService(mode=mode, bus=self.bus)
        self.engine = DefaultPermissionEngine(
            profiles=YamlProfileProvider(PROFILES_DIR),
            privacy=self.privacy,
            initial_profile=profile_id,
        )
        self.policy = EffectivePolicy(privacy=self.privacy, profile=self.engine.active_profile)
        self.ollama = FakeProvider("ollama", local=True)
        self.claude = FakeProvider("claude_code", local=False)
        self.rules = FakeProvider("rules", local=True, roles=[AiRole.CHAT, AiRole.CLASSIFY])
        ai_config = AiConfig.from_mapping(_defaults().ai.model_dump())
        self.router = build_router(
            [self.rules, self.ollama, self.claude], ai_config, bus=self.bus, policy=self.policy
        )

    async def answer(self, text: str, *, role: AiRole) -> str:
        request = AiRequest(
            request_id=f"req-{role.value}",
            role=role,
            messages=[Message(role="user", content=text)],
            privacy_mode=self.privacy.mode.value,
            timeout_s=5.0,
        )
        response = await self.router.complete(request)
        return response.provider


class _Repo:
    """Stands in for `MemoryItemRepository`; records what reached the database."""

    def __init__(self) -> None:
        self.rows: list[str] = []

    def add(self, **fields: Any) -> Any:
        self.rows.append(str(fields["text"]))
        return type("Row", (), {"id": len(self.rows), "type": fields["type"], **fields})()


class _NoSecrets:
    def get(self, name: str) -> str | None:
        return None


def _plugin_manager(setup: _Setup, enabled: Sequence[str]) -> tuple[PluginManager, list[str]]:
    spawned: list[str] = []
    config = _defaults()
    manager = PluginManager(
        plugins_dir=PLUGINS_DIR,
        bus=setup.bus,
        hub=FakeHub(),
        tokens=FakeTokens(),
        registry=RequestRegistry(),
        engine=setup.engine,
        secrets=_NoSecrets(),
        settings=PluginManagerSettings(enabled=list(enabled), poll_interval_s=60.0),
        global_egress_allowlist=tuple(config.security.egress_allowlist),
        loopback_allowlist=tuple(config.security.loopback_allowlist),
        start_policy=setup.policy,
    )

    def factory(command: Sequence[str], _env: Mapping[str, str]) -> FakeProcess:
        spawned.append(command[command.index("--plugin") + 1])
        return FakeProcess(pid=2000 + len(spawned))

    manager._process_factory = factory  # type: ignore[assignment]
    return manager, spawned


# ---- the matrix --------------------------------------------------------------------------------


@pytest.mark.parametrize(("profile_id", "mode"), CELLS, ids=CELL_IDS)
async def test_chat_reaches_the_cloud_only_where_profile_and_privacy_both_allow_it(
    profile_id: str, mode: PrivacyMode
) -> None:
    setup = _Setup(profile_id, mode)
    setup.ollama.status = HealthStatus.UNAVAILABLE  # the case that used to fall through to cloud
    expected_cloud = PROMISES[profile_id].cloud and mode not in LOCAL_ONLY

    provider = await setup.answer("Hallo Nox, was meinst du dazu?", role=AiRole.CHAT)

    assert setup.policy.cloud_allowed() is expected_cloud
    assert provider == ("claude_code" if expected_cloud else "rules")
    assert (setup.claude.complete_calls > 0) is expected_cloud


@pytest.mark.parametrize(("profile_id", "mode"), CELLS, ids=CELL_IDS)
async def test_an_escalated_turn_goes_to_the_cloud_only_where_both_allow_it(
    profile_id: str, mode: PrivacyMode
) -> None:
    setup = _Setup(profile_id, mode)
    text = "Denk mal gründlich nach, warum mein Backup seit dem Update abbricht."
    decision = EscalationPolicy().decide(text, relevance=0.0)
    assert decision.role is AiRole.REASON
    expected_cloud = PROMISES[profile_id].cloud and mode not in LOCAL_ONLY

    provider = await setup.answer(text, role=decision.role)

    assert provider == ("claude_code" if expected_cloud else "ollama")
    assert (setup.claude.complete_calls > 0) is expected_cloud


@pytest.mark.parametrize(("profile_id", "mode"), CELLS, ids=CELL_IDS)
async def test_memory_is_written_only_where_profile_and_privacy_both_allow_it(
    profile_id: str, mode: PrivacyMode
) -> None:
    setup = _Setup(profile_id, mode)
    repo = _Repo()
    memory = MemoryService(repo, setup.policy)  # type: ignore[arg-type]
    expected = PROMISES[profile_id].memory and mode is not PrivacyMode.PRIVATE

    assert setup.policy.allows_memory_write() is expected
    if expected:
        await memory.create("Die Katze heißt Mila.", source="test")
        assert repo.rows == ["Die Katze heißt Mila."]
    else:
        with pytest.raises(MemoryWriteRefusedError):
            await memory.create("Die Katze heißt Mila.", source="test")
        assert repo.rows == []


@pytest.mark.parametrize(("profile_id", "mode"), CELLS, ids=CELL_IDS)
async def test_plugins_start_only_where_profile_and_privacy_both_allow_it(
    profile_id: str, mode: PrivacyMode
) -> None:
    setup = _Setup(profile_id, mode)
    manager, spawned = _plugin_manager(setup, SHIPPED_PLUGINS)
    expected = set(PROMISES[profile_id].plugins)
    if mode in LOCAL_ONLY:
        expected -= OFF_THE_MACHINE
    try:
        await manager.start()
        assert set(spawned) == expected
    finally:
        await manager.stop("test")


# ---- switching at runtime ----------------------------------------------------------------------


async def test_a_profile_switch_changes_every_answer_without_a_restart() -> None:
    setup = _Setup("companion", PrivacyMode.BALANCED)
    setup.ollama.status = HealthStatus.UNAVAILABLE
    manager, spawned = _plugin_manager(setup, SHIPPED_PLUGINS)
    try:
        await manager.start()
        assert "telegram" in spawned
        assert await setup.answer("Hallo", role=AiRole.CHAT) == "claude_code"

        setup.engine.set_profile("work", by="test")
        await manager.apply_profile()  # what `system.mode_changed` triggers in the core

        assert await setup.answer("Hallo", role=AiRole.CHAT) == "rules"
        assert not setup.policy.allows_memory_write()
        states = {pid: rec.state for pid, rec in manager.records().items()}
        assert states["telegram"] is PluginState.STOPPED
        assert states["home"] is PluginState.STOPPED
    finally:
        await manager.stop("test")


async def test_a_privacy_change_stops_and_restarts_network_plugins_by_itself() -> None:
    setup = _Setup("companion", PrivacyMode.BALANCED)
    manager, spawned = _plugin_manager(setup, SHIPPED_PLUGINS)
    try:
        await manager.start()
        record = manager.records()["telegram"]
        assert record.state is PluginState.SPAWNED

        await setup.privacy.set_mode(PrivacyMode.OFFLINE, by="test")  # publishes the change
        assert record.state is PluginState.STOPPED
        assert "offline" in record.reason
        assert manager.records()["echo"].state is PluginState.SPAWNED  # local plugins stay

        await setup.privacy.set_mode(PrivacyMode.BALANCED, by="test")
        assert record.state is PluginState.SPAWNED
        assert spawned.count("telegram") == 2
    finally:
        await manager.stop("test")


async def test_the_router_reads_the_live_privacy_state_not_the_requests_copy() -> None:
    """A request built a moment before the switch to OFFLINE still says BALANCED; the policy must
    decide from the live state, so nothing leaves the machine."""
    setup = _Setup("companion", PrivacyMode.BALANCED)
    setup.ollama.status = HealthStatus.UNAVAILABLE
    stale = AiRequest(
        request_id="stale",
        role=AiRole.CHAT,
        messages=[Message(role="user", content="Hallo")],
        privacy_mode="balanced",
        timeout_s=5.0,
    )
    await setup.privacy.set_mode(PrivacyMode.OFFLINE, by="test")
    response = await setup.router.complete(stale)
    assert response.provider == "rules"
    assert setup.claude.complete_calls == 0
    assert "privacy mode offline" in setup.router.explain("stale")
