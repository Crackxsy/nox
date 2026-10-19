"""`SkillService`: learn, check, test, ready, one-click activate, and everything around it."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from nox.core.config.hands import SkillsConfig
from nox.plugins.manifest import PluginManifest
from nox.skills.service import SkillService, SkillState, skill_id_for
from nox.skills.testing import UnavailableTestRunner
from tests._fake_skill_builder import FakeSkillBuilder
from tests._local_skill_test_runner import LocalTestRunner


class Plugins:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.allowed: list[str] = []
        self.egress: dict[str, list[str]] = {}

    def allow_egress(self, plugin_id: str, entries: Iterable[str]) -> None:
        self.allowed += list(entries)
        self.egress[plugin_id] = list(entries)

    def revoke_egress(self, plugin_id: str) -> None:
        self.egress.pop(plugin_id, None)

    async def rescan(self) -> None:
        self.calls.append(("rescan", None))

    async def enable(self, plugin_id: str) -> Any:
        self.calls.append(("enable", plugin_id))
        return None

    async def disable(self, plugin_id: str, reason: str = "disabled") -> None:
        self.calls.append(("disable", plugin_id))

    async def remove(self, plugin_id: str) -> None:
        self.calls.append(("remove", plugin_id))


class Gate:
    def __init__(self, required: bool = True) -> None:
        self.required = required
        self.asked: list[str | None] = []

    def is_required(self) -> bool:
        return self.required

    async def require(self, pin: str | None, *, action: str, by: str) -> None:
        self.asked.append(pin)
        if pin != "1234":
            raise PermissionError(f"{action} requires the security PIN")


class Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append(self, **row: Any) -> None:
        self.rows.append(row)


def service(
    tmp_path: Path,
    builder: FakeSkillBuilder,
    *,
    gate: Gate | None = None,
    allowed_hosts: tuple[str, ...] = (),
    tests: Any = None,
    runtime: str | None = None,
    revoked: list[str] | None = None,
) -> tuple[SkillService, Plugins, list[tuple[str, bool]], list[str], Audit]:
    plugins = Plugins()
    enabled: list[tuple[str, bool]] = []
    told: list[str] = []
    audit = Audit()

    async def persist(plugin_id: str, on: bool) -> None:
        enabled.append((plugin_id, on))

    async def notify(text: str, _key: str) -> None:
        told.append(text)

    def new_hosts(manifest: PluginManifest) -> list[str]:
        return [e for e in manifest.network.egress if e not in allowed_hosts]

    svc = SkillService(
        root=tmp_path / "skills",
        plugins_root=tmp_path / "plugins",
        builder=builder,
        tests=tests or LocalTestRunner(),
        config=lambda: SkillsConfig(test_timeout_s=60),
        plugins=plugins,
        taken_namespaces=lambda: {"file", "web", "skill"},
        new_hosts=new_hosts,
        gate=gate,
        persist_enabled=persist,
        notify=notify,
        audit=audit,
        runtime_unavailable=lambda: runtime,
        revoke_grants=lambda prefix: (revoked.append(prefix) if revoked is not None else None) or 0,
    )
    return svc, plugins, enabled, told, audit


async def test_learn_check_test_activate_deactivate_forget(tmp_path: Path) -> None:
    svc, plugins, enabled, told, audit = service(tmp_path, FakeSkillBuilder())
    started = await svc.learn("Wuerfel fuer mich", capability="dice.roll", by="companion")
    assert started["ok"] and started["id"] == "dice_roll"
    await svc.wait()
    record = svc.get("dice_roll")
    assert record is not None and record.state is SkillState.READY, record
    assert record.summary is not None and record.summary.tests_passed == 2
    assert [t.name for t in record.summary.tools] == ["dice_roll.roll"]
    assert not record.summary.needs_pin and told and "beigebracht" in told[0]
    assert (tmp_path / "skills" / "dice_roll" / "versions" / "1" / "manifest.yaml").is_file()
    assert not (tmp_path / "plugins" / "dice_roll").exists()  # nothing runs before the click

    activated = await svc.activate("dice_roll", pin=None, by="dashboard")
    assert activated["ok"]
    assert (tmp_path / "plugins" / "dice_roll" / "manifest.yaml").is_file()
    assert enabled == [("dice_roll", True)] and ("enable", "dice_roll") in plugins.calls

    assert (await svc.deactivate("dice_roll", by="dashboard"))["ok"]
    assert not (tmp_path / "plugins" / "dice_roll").exists()
    assert (await svc.forget("dice_roll", by="dashboard"))["ok"]
    assert svc.get("dice_roll") is None and not (tmp_path / "skills" / "dice_roll").exists()
    actions = [row["action"] for row in audit.rows]
    assert actions == [
        "skill.learn",
        "skill.build",
        "skill.activate",
        "skill.deactivate",
        "skill.forget",
    ]


async def test_problems_go_back_to_the_builder_and_it_repairs(tmp_path: Path) -> None:
    builder = FakeSkillBuilder(rounds=["forbidden_api", "failing_tests", "good"])
    svc, *_ = service(tmp_path, builder)
    await svc.learn("Wuerfeln", capability="dice", by="companion")
    await svc.wait()
    record = svc.get("dice")
    assert record is not None and record.state is SkillState.READY and record.rounds == 3
    assert any("subprocess" in f for f in builder.requests[1].feedback)
    assert any("tests:" in f for f in builder.requests[2].feedback)


async def test_it_gives_up_after_three_repair_rounds(tmp_path: Path) -> None:
    builder = FakeSkillBuilder(rounds=["hard_prohibition"])
    svc, plugins, _enabled, told, _audit = service(tmp_path, builder)
    await svc.learn("Wuerfeln", capability="dice", by="companion")
    await svc.wait()
    record = svc.get("dice")
    assert record is not None and record.state is SkillState.FAILED
    assert len(builder.requests) == 4 and any("hard prohibition" in p for p in record.problems)
    assert (await svc.activate("dice", pin=None, by="dashboard"))["ok"] is False
    assert plugins.calls == [] and "nicht" in told[-1]


async def test_a_new_host_needs_the_pin_and_is_approved_once(tmp_path: Path) -> None:
    gate = Gate()
    svc, plugins, *_ = service(tmp_path, FakeSkillBuilder(rounds=["with_host"]), gate=gate)
    await svc.learn("Wuerfeln online", capability="dice", by="companion")
    await svc.wait()
    record = svc.get("dice")
    assert record is not None and record.summary is not None and record.summary.needs_pin
    refused = await svc.activate("dice", pin=None, by="dashboard")
    assert refused["ok"] is False and refused["needs_pin"] and plugins.calls == []
    assert (await svc.activate("dice", pin="1234", by="dashboard"))["ok"]
    assert plugins.allowed == ["api.example.org:443"]


async def test_a_host_on_the_allow_list_needs_no_pin(tmp_path: Path) -> None:
    gate = Gate()
    svc, *_ = service(
        tmp_path,
        FakeSkillBuilder(rounds=["with_host"]),
        gate=gate,
        allowed_hosts=("api.example.org:443",),
    )
    await svc.learn("Wuerfeln online", capability="dice", by="companion")
    await svc.wait()
    assert (await svc.activate("dice", pin=None, by="dashboard"))["ok"] and gate.asked == []


async def test_files_changed_after_the_check_are_never_activated(tmp_path: Path) -> None:
    svc, plugins, *_ = service(tmp_path, FakeSkillBuilder())
    await svc.learn("Wuerfeln", capability="dice", by="companion")
    await svc.wait()
    source = tmp_path / "skills" / "dice" / "versions" / "1" / "src" / "nox_skill_dice"
    (source / "__init__.py").write_text("import subprocess\n", encoding="utf-8")
    answer = await svc.activate("dice", pin=None, by="dashboard")
    assert answer["ok"] is False and "changed" in answer["reason"] and plugins.calls == []


async def test_without_a_builder_learning_is_honestly_impossible(tmp_path: Path) -> None:
    svc, *_ = service(tmp_path, FakeSkillBuilder(reason="Claude Code is not installed"))
    answer = await svc.learn("Wuerfeln", capability="dice", by="companion")
    assert answer == {"ok": False, "reason": "Claude Code is not installed"}
    assert svc.records() == []


async def test_without_a_sandbox_nothing_is_learned_or_run(tmp_path: Path) -> None:
    builder = FakeSkillBuilder()
    svc, *_ = service(tmp_path, builder, tests=UnavailableTestRunner())
    answer = await svc.learn("Wuerfeln", capability="dice", by="companion")
    assert answer["ok"] is False and "sandbox" in answer["reason"]
    assert builder.requests == [] and svc.records() == []


async def test_without_a_sandboxed_runtime_nothing_is_switched_on(tmp_path: Path) -> None:
    svc, plugins, enabled, *_ = service(tmp_path, FakeSkillBuilder(), runtime="no sandbox here")
    await svc.learn("Wuerfeln", capability="dice", by="companion")
    await svc.wait()
    answer = await svc.activate("dice", pin=None, by="dashboard")
    assert answer == {"ok": False, "reason": "no sandbox here"}
    assert plugins.calls == [] and enabled == []
    assert not (tmp_path / "plugins" / "dice").exists()


async def test_halt_stops_a_build_and_blocks_activation(tmp_path: Path) -> None:
    svc, plugins, *_ = service(tmp_path, FakeSkillBuilder())
    await svc.learn("Wuerfeln", capability="dice", by="companion")
    await svc.halt()
    assert (await svc.learn("Noch was", by="companion"))["ok"] is False
    svc.resume()
    record = svc.get("dice")
    assert record is not None and record.state in (SkillState.FAILED, SkillState.READY)


def test_restart_closes_interrupted_builds_and_reapproves_hosts(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from nox.skills.service import SkillRecord

    svc, plugins, *_ = service(tmp_path, FakeSkillBuilder())
    now = datetime.now(UTC)
    for sid, state, hosts in (
        ("a", SkillState.BUILDING, []),
        ("b", SkillState.ACTIVE, ["x.org:443"]),
    ):
        svc._save(  # noqa: SLF001 - seeding records as a previous run left them
            SkillRecord(
                id=sid,
                intent="i",
                state=state,
                created_at=now,
                updated_at=now,
                approved_hosts=hosts,
            )
        )
    svc.start()
    assert plugins.allowed == ["x.org:443"] and plugins.egress == {"b": ["x.org:443"]}
    record = svc.get("a")
    assert record is not None and record.state is SkillState.FAILED


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("weather.forecast", "weather_forecast"),
        ("Würfel für mich", "wurfel_fur_mich"),
        ("home.unlock", "skill_home_unlock"),
        ("", "skill"),
        ("123", "skill"),
    ],
)
def test_skill_ids(text: str, expected: str) -> None:
    assert skill_id_for(text, taken=()) == expected


def test_skill_ids_avoid_taken_names() -> None:
    assert skill_id_for("dice", taken={"dice", "dice_2"}) == "dice_3"


async def test_approved_hosts_and_remembered_grants_end_with_the_skill(tmp_path: Path) -> None:
    revoked: list[str] = []
    svc, plugins, *_ = service(
        tmp_path, FakeSkillBuilder(rounds=["with_host"]), gate=Gate(), revoked=revoked
    )
    await svc.learn("Wuerfeln online", capability="dice", by="companion")
    await svc.wait()
    assert (await svc.activate("dice", pin="1234", by="dashboard"))["ok"]
    assert plugins.egress == {"dice": ["api.example.org:443"]}  # for this skill only
    assert revoked == ["session:dice/"]  # a new version starts without old "remember"s
    assert (await svc.deactivate("dice", by="dashboard"))["ok"]
    assert plugins.egress == {} and revoked[-1] == "session:dice/"
    assert (await svc.activate("dice", pin="1234", by="dashboard"))["ok"]
    assert (await svc.forget("dice", by="dashboard"))["ok"]
    assert plugins.egress == {} and revoked == ["session:dice/"] * 4
