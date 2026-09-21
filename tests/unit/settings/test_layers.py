"""`user.yaml` is written round-trip (#22): a hand-edited file survives a `config.set`.

The point of these tests is what a person opening `user.yaml` afterwards sees: their comments,
their key order, their quoting and every key Nox does not know about, all still there - and only
the one value they changed in the dashboard actually changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nox.core.config import NoxConfig, read_yaml_layer
from nox.settings.layers import (
    UserConfigError,
    load_existing_user_layer,
    write_user_config,
)

COMMENTED_USER_CONFIG = """\
# my own notes about this file
identity:
  # the name I picked
  name: "Nox"
  ui_language: de     # de, because I am German
experimental_thing:   # not a key Nox knows
  keep: me
stream:
  twitch:
    channel: mychannel
# last line of the file
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_a_config_set_keeps_comments_unknown_keys_and_changes_one_value(user_config: Path) -> None:
    write(user_config, COMMENTED_USER_CONFIG)

    write_user_config(user_config, {"identity": {"name": "Luna"}})

    text = read(user_config)
    assert "# my own notes about this file" in text
    assert "# the name I picked" in text
    assert "# de, because I am German" in text
    assert "# not a key Nox knows" in text
    assert "# last line of the file" in text
    # The written value changed, the quoting style of the line it lives on did not.
    assert 'name: "Luna"' in text
    assert '"Nox"' not in text

    data = load_existing_user_layer(user_config)
    assert data["identity"] == {"name": "Luna", "ui_language": "de"}
    assert data["experimental_thing"] == {"keep": "me"}
    assert data["stream"]["twitch"]["channel"] == "mychannel"


def test_key_order_is_the_users_order_not_an_alphabetical_one(user_config: Path) -> None:
    write(user_config, COMMENTED_USER_CONFIG)

    write_user_config(user_config, {"identity": {"ui_language": "en"}})

    lines = [line for line in read(user_config).splitlines() if line and not line.startswith("#")]
    assert lines[0] == "identity:"
    assert [line for line in lines if line.startswith("experimental_thing")] == [
        "experimental_thing:   # not a key Nox knows"
    ]
    assert lines.index("stream:") > lines.index("experimental_thing:   # not a key Nox knows")


def test_the_file_stays_readable_for_the_plain_safe_load_loader(user_config: Path) -> None:
    write(user_config, COMMENTED_USER_CONFIG)

    write_user_config(user_config, {"identity": {"name": "Luna"}})

    # Every other reader in the code base uses `yaml.safe_load` (`nox.core.config`), so what the
    # round-trip writer emits has to be plain YAML, not a ruamel-only dialect.
    loaded = yaml.safe_load(read(user_config))
    assert loaded["identity"]["name"] == "Luna"
    assert read_yaml_layer(user_config)["identity"]["name"] == "Luna"


def test_a_missing_file_is_created_with_the_header(user_config: Path) -> None:
    assert not user_config.exists()

    write_user_config(user_config, {"identity": {"name": "Nox"}})

    text = read(user_config)
    assert text.startswith("# Nox user configuration")
    assert yaml.safe_load(text) == {"identity": {"name": "Nox"}}


def test_an_empty_file_is_filled_without_losing_its_own_comments(user_config: Path) -> None:
    write(user_config, "# I emptied this file on purpose\n")

    write_user_config(user_config, {"identity": {"name": "Nox"}})

    text = read(user_config)
    assert "# I emptied this file on purpose" in text
    assert yaml.safe_load(text) == {"identity": {"name": "Nox"}}


def test_the_header_is_written_once_no_matter_how_often_the_file_is_written(
    user_config: Path,
) -> None:
    write_user_config(user_config, {"identity": {"name": "Nox"}})
    write_user_config(user_config, {"identity": {"ui_language": "en"}})
    write_user_config(user_config, {"pet": {"variant": "neutral"}})

    assert read(user_config).count("# Nox user configuration") == 1


def test_invalid_yaml_is_a_clear_error_and_the_file_is_left_untouched(user_config: Path) -> None:
    broken = "identity:\n  name: [unclosed\n"
    write(user_config, broken)

    with pytest.raises(UserConfigError) as excinfo:
        write_user_config(user_config, {"identity": {"name": "Luna"}})

    assert str(user_config) in str(excinfo.value)
    assert read(user_config) == broken


def test_reading_an_invalid_file_raises_instead_of_reporting_an_empty_layer(
    user_config: Path,
) -> None:
    # `config.set` merges its patch into whatever this returns. Answering "{}" for a file that
    # merely failed to parse would quietly drop everything the user had in it on the next write.
    write(user_config, "identity: [unclosed\n")

    with pytest.raises(UserConfigError):
        load_existing_user_layer(user_config)


def test_lists_and_nested_maps_survive_a_write(user_config: Path) -> None:
    write(
        user_config,
        "plugins:\n"
        "  enabled:\n"
        "    - twitch     # the stream bot\n"
        "    - obs\n"
        "ai:\n"
        "  router:\n"
        "    fallback_chain: [claude_code, ollama, rules]\n",
    )

    write_user_config(user_config, {"plugins": {"enabled": ["twitch"]}})

    text = read(user_config)
    assert "fallback_chain: [claude_code, ollama, rules]" in text  # flow style kept
    data = load_existing_user_layer(user_config)
    assert data["plugins"]["enabled"] == ["twitch"]
    assert data["ai"]["router"]["fallback_chain"] == ["claude_code", "ollama", "rules"]


def test_what_was_written_still_validates_as_a_user_layer(
    user_config: Path, defaults_path: Path
) -> None:
    write(user_config, COMMENTED_USER_CONFIG)

    write_user_config(user_config, {"stream": {"twitch": {"channel": "otherchannel"}}})

    merged = {**read_yaml_layer(defaults_path)}
    user = load_existing_user_layer(user_config)
    merged["identity"] = {**merged["identity"], **user["identity"]}
    merged["stream"]["twitch"] = {**merged["stream"]["twitch"], **user["stream"]["twitch"]}
    config = NoxConfig.model_validate(merged)
    assert config.stream.twitch.channel == "otherchannel"
