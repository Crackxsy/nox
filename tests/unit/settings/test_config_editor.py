"""`config.get` / `config.set`: allow-list, validation, live apply vs restart, audit and event."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from nox.core.config import NoxConfig
from nox.settings.editor import ConfigEditor
from nox.settings.schema import EDITABLE_PATHS
from tests.unit.fakes import FakeBus
from tests.unit.settings.conftest import FakeAudit


# Plain sync helpers: file IO inside an async test is flagged (ASYNC240), and these assertions
# are about what ended up on disk, not about doing IO concurrently.
def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def file_exists(path: Path) -> bool:
    return path.exists()


def file_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def make_editor(
    config: NoxConfig,
    defaults_path: Path,
    user_config: Path,
    audit: FakeAudit,
    bus: FakeBus,
    appliers: dict[str, Any] | None = None,
) -> ConfigEditor:
    return ConfigEditor(
        config=config,
        defaults_path=defaults_path,
        user_config_path=user_config,
        appliers=appliers or {},
        audit=audit,
        bus=bus,
    )


def test_snapshot_reports_values_schema_and_the_file_it_writes(
    config: NoxConfig, defaults_path: Path, user_config: Path, audit: FakeAudit, bus: FakeBus
) -> None:
    snapshot = make_editor(config, defaults_path, user_config, audit, bus).snapshot()

    assert set(snapshot["values"]) == set(EDITABLE_PATHS)
    assert {entry["path"] for entry in snapshot["schema"]} == set(EDITABLE_PATHS)
    assert snapshot["user_config_path"] == str(user_config)
    assert snapshot["values"]["identity.ui_language"] == "de"


async def test_set_writes_only_the_user_layer_and_reports_restart_required(
    config: NoxConfig, defaults_path: Path, user_config: Path, audit: FakeAudit, bus: FakeBus
) -> None:
    editor = make_editor(config, defaults_path, user_config, audit, bus)

    result = await editor.apply({"voice.stt.wake_word": "Luna"}, by="dashboard")

    assert result["ok"] is True
    assert result["applied"] == []
    assert result["restart_required"] == ["voice.stt.wake_word"]
    written = read_yaml(user_config)
    assert written == {"voice": {"stt": {"wake_word": "Luna"}}}
    # The Defaults layer is never written to.
    assert "Luna" not in file_text(defaults_path)


async def test_live_applied_paths_run_their_applier_and_are_reported_as_applied(
    config: NoxConfig, defaults_path: Path, user_config: Path, audit: FakeAudit, bus: FakeBus
) -> None:
    seen: list[Any] = []

    async def applier(value: Any) -> None:
        seen.append(value)

    editor = make_editor(config, defaults_path, user_config, audit, bus, {"pet.variant": applier})

    result = await editor.apply({"pet.variant": "fox"})

    assert result["applied"] == ["pet.variant"]
    assert result["restart_required"] == []
    assert seen == ["fox"]


async def test_an_invalid_value_is_reported_per_path_and_never_reaches_the_file(
    config: NoxConfig, defaults_path: Path, user_config: Path, audit: FakeAudit, bus: FakeBus
) -> None:
    editor = make_editor(config, defaults_path, user_config, audit, bus)

    result = await editor.apply({"identity.ui_language": "klingon", "voice.stt.wake_word": "Luna"})

    assert result["ok"] is False
    assert "identity.ui_language" in result["errors"]
    assert result["restart_required"] == ["voice.stt.wake_word"]
    written = read_yaml(user_config)
    assert written == {"voice": {"stt": {"wake_word": "Luna"}}}  # the good half still applied


async def test_a_value_outside_a_numeric_bound_is_rejected(
    config: NoxConfig, defaults_path: Path, user_config: Path, audit: FakeAudit, bus: FakeBus
) -> None:
    editor = make_editor(config, defaults_path, user_config, audit, bus)

    result = await editor.apply({"voice.tts.rate": 99.0})

    assert result["ok"] is False
    assert "voice.tts.rate" in result["errors"]
    assert not file_exists(user_config)


async def test_paths_outside_the_allow_list_are_refused(
    config: NoxConfig, defaults_path: Path, user_config: Path, audit: FakeAudit, bus: FakeBus
) -> None:
    editor = make_editor(config, defaults_path, user_config, audit, bus)

    result = await editor.apply(
        {
            "security.hard_prohibitions": [],
            "supervisor.core_command": ["cmd.exe"],
            "paths.data_dir": "C:/somewhere",
        }
    )

    assert result["ok"] is False
    assert set(result["errors"]) == {
        "security.hard_prohibitions",
        "supervisor.core_command",
        "paths.data_dir",
    }
    assert not file_exists(user_config)
    assert audit.entries == []  # nothing changed, so nothing is audited


async def test_unknown_keys_in_user_yaml_survive_a_write(
    config: NoxConfig, defaults_path: Path, user_config: Path, audit: FakeAudit, bus: FakeBus
) -> None:
    write_yaml(user_config, {"identity": {"name": "Nox"}, "experimental": {"flag": True}})
    editor = make_editor(config, defaults_path, user_config, audit, bus)

    await editor.apply({"voice.stt.wake_word": "Luna"})

    written = read_yaml(user_config)
    assert written["experimental"] == {"flag": True}
    assert written["identity"] == {"name": "Nox"}


async def test_audit_and_event_carry_paths_but_never_values(
    config: NoxConfig, defaults_path: Path, user_config: Path, audit: FakeAudit, bus: FakeBus
) -> None:
    editor = make_editor(config, defaults_path, user_config, audit, bus)

    await editor.apply({"identity.user_display_name": "Some Private Name"})

    entry = audit.entries[-1]
    assert entry["action"] == "config.set"
    assert entry["target"] == "identity.user_display_name"
    assert "Some Private Name" not in audit.blob()

    events = [e for e in bus.published if e.name == "settings.changed"]
    assert len(events) == 1
    assert events[0].payload == {"paths": ["identity.user_display_name"]}
    assert "Some Private Name" not in repr(events[0].payload)


async def test_nothing_is_published_when_every_path_was_rejected(
    config: NoxConfig, defaults_path: Path, user_config: Path, audit: FakeAudit, bus: FakeBus
) -> None:
    editor = make_editor(config, defaults_path, user_config, audit, bus)

    await editor.apply({"identity.ui_language": "klingon"})

    assert [e.name for e in bus.published] == []
