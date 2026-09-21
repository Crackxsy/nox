"""What the first-run wizard promises: it writes only the user layer, every step can be skipped,
a credential never lands in a file, and `config/defaults.yaml` stays byte-identical.

The interactive tests drive the real `nox onboard` command. The language question comes first and
everything after it is in that language, so the two full-flow tests below run one language each.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import nox.onboarding.wizard as wizard
from nox.core.events import HealthStatus
from nox.onboarding.wizard import (
    AiProbeResult,
    OnboardingAnswers,
    build_user_layer_patch,
    default_user_config_path,
    load_existing_user_layer,
    render_capability_summary,
    store_secrets,
    write_user_config,
)
from nox.security.secrets import InMemorySecretStore

runner = CliRunner()


# ---- pure helpers -----------------------------------------------------------------------------


def test_default_user_config_path_uses_appdata_param() -> None:
    assert default_user_config_path(Path("C:/tmp/appdata")) == Path("C:/tmp/appdata/Nox/user.yaml")


def test_default_user_config_path_uses_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", "C:/env/appdata")
    assert default_user_config_path() == Path("C:/env/appdata/Nox/user.yaml")


def test_build_user_layer_patch_empty_for_declined_wizard() -> None:
    assert build_user_layer_patch(OnboardingAnswers()) == {}


def test_build_user_layer_patch_maps_every_field() -> None:
    answers = OnboardingAnswers(
        nox_name="Kumo",
        user_display_name="Alex",
        ui_language="en",
        data_dir=Path("D:/nox-data"),
        vault_dir=Path("D:/nox-vault"),
        microphone_enabled=False,
        camera_enabled=True,
        ai_backend="ollama",
    )
    patch = build_user_layer_patch(answers)
    assert patch["identity"] == {"name": "Kumo", "user_display_name": "Alex", "ui_language": "en"}
    assert patch["paths"] == {
        "data_dir": str(Path("D:/nox-data")),
        "vault_dir": str(Path("D:/nox-vault")),
    }
    assert patch["privacy"] == {"capture": {"microphone": False, "camera": True}}
    assert patch["ai"]["router"]["default_reasoner"] == "ollama"
    assert patch["ai"]["providers"]["claude_code"]["enabled"] is False


def test_build_user_layer_patch_claude_code_backend() -> None:
    patch = build_user_layer_patch(OnboardingAnswers(ai_backend="claude_code"))
    assert patch["ai"]["router"]["default_reasoner"] == "claude_code"
    assert patch["ai"]["providers"]["claude_code"]["enabled"] is True


def test_write_user_config_writes_and_merges(tmp_path: Path) -> None:
    target = tmp_path / "Nox" / "user.yaml"
    write_user_config(target, {"identity": {"name": "First"}})
    write_user_config(target, {"identity": {"ui_language": "en"}})

    written = load_existing_user_layer(target)
    assert written["identity"] == {"name": "First", "ui_language": "en"}


def test_write_user_config_never_contains_secret_shaped_values(tmp_path: Path) -> None:
    target = tmp_path / "Nox" / "user.yaml"
    answers = OnboardingAnswers(nox_name="Nox", twitch=("oauth:abc123", "mybot"))
    patch = build_user_layer_patch(answers)
    write_user_config(target, patch)
    text = target.read_text(encoding="utf-8")
    assert "oauth:abc123" not in text
    assert "mybot" not in text


def test_store_secrets_only_writes_provided_ones() -> None:
    store = InMemorySecretStore()
    answers = OnboardingAnswers(twitch=("oauth:abc123", "mybot"), obs_password="s3cret-pw")
    stored = store_secrets(answers, store)
    assert set(stored) == {
        "nox/twitch/oauth_token",
        "nox/twitch/bot_username",
        "nox/obs/websocket_password",
    }
    assert store.get("nox/twitch/oauth_token") == "oauth:abc123"
    assert store.get("nox/telegram/bot_token") is None


def test_store_secrets_empty_when_all_declined() -> None:
    store = InMemorySecretStore()
    assert store_secrets(OnboardingAnswers(), store) == []


def test_render_capability_summary_is_honest_about_missing_backends() -> None:
    summary = render_capability_summary(
        answers=OnboardingAnswers(
            ai_backend="ollama",
            microphone_enabled=True,
            camera_enabled=False,
            ui_language="en",
        ),
        ai_probes=[
            AiProbeResult(
                id="claude_code", status=HealthStatus.UNAVAILABLE, reason="not logged in"
            ),
            AiProbeResult(id="ollama", status=HealthStatus.AVAILABLE, reason="model present"),
        ],
        stored_secret_names=[],
        user_config_path=Path("C:/appdata/Nox/user.yaml"),
    )
    assert "claude_code: unavailable (not logged in)" in summary
    assert "ollama: available (model present)" in summary
    assert "credentials     : none" in summary
    # The summary has to say what to do next, not just what was written.
    assert "nox supervisor" in summary
    assert "http://127.0.0.1:47801/dashboard" in summary


# ---- Defaults layer must never change --------------------------------------------------------


def test_defaults_yaml_is_never_touched_by_any_helper(tmp_path: Path) -> None:
    before = wizard.DEFAULTS_PATH.read_bytes()
    target = tmp_path / "Nox" / "user.yaml"
    answers = OnboardingAnswers(nox_name="Kumo", ai_backend="ollama", microphone_enabled=False)
    write_user_config(target, build_user_layer_patch(answers))
    after = wizard.DEFAULTS_PATH.read_bytes()
    assert before == after


# ---- full interactive flow via the real `nox onboard` CLI command -----------------------------


async def _fake_probe() -> list[AiProbeResult]:
    return [
        AiProbeResult(id="claude_code", status=HealthStatus.AVAILABLE, reason="ok"),
        AiProbeResult(id="ollama", status=HealthStatus.UNAVAILABLE, reason="server not running"),
    ]


def _run_onboard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdin: str) -> tuple[int, str]:
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(wizard, "default_secret_store", lambda: InMemorySecretStore())
    monkeypatch.setattr(wizard, "probe_ai_backends", _fake_probe)

    from nox.cli import app

    result = runner.invoke(app, ["onboard"], input=stdin)
    return result.exit_code, result.output


#: Every question in order, so a test reads as the conversation it drives.
#: language, Nox name, your name, data folder, vault folder, Twitch by hand?, OBS?, Telegram?,
#: microphone?, camera?, probe backends?
SKIP_EVERYTHING = "\n" * 5 + "n\nn\nn\n" + "\n\n" + "n\n"


def test_onboard_cli_skip_everything_writes_only_confirmed_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exit_code, output = _run_onboard(monkeypatch, tmp_path, SKIP_EVERYTHING)
    assert exit_code == 0, output

    written = load_existing_user_layer(default_user_config_path(tmp_path / "AppData" / "Roaming"))
    assert written["identity"]["name"] == "Nox"
    assert written["identity"]["ui_language"] == "de"
    assert written["privacy"]["capture"] == {"microphone": True, "camera": False}
    assert "ai" not in written  # the probe was declined, so the defaults layer's choice applies
    assert "Zugangsdaten    : keine" in output


def test_onboard_cli_in_german_asks_and_answers_in_german_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A German run contains no English prompt, and the other way round.

    Half a wizard in each language is what a first start must never look like.
    """
    _, german = _run_onboard(monkeypatch, tmp_path, SKIP_EVERYTHING)
    assert "Datenordner" in german and "Vault-Ordner" in german
    assert "So geht es weiter:" in german and "nox supervisor" in german
    assert "Data folder" not in german and "What happens next" not in german

    english_input = "en\n" + "\n" * 4 + "n\nn\nn\n" + "\n\n" + "n\n"
    _, english = _run_onboard(monkeypatch, tmp_path, english_input)
    assert "Data folder" in english and "Vault folder" in english
    assert "What happens next:" in english
    assert "Datenordner" not in english and "So geht es weiter" not in english


def test_onboard_cli_explains_both_folders_before_asking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, output = _run_onboard(monkeypatch, tmp_path, SKIP_EVERYTHING)
    assert "Datenbank, Modelle, Logs und Backups" in output
    assert "deine Notizen als Markdown-Dateien" in output
    # The explanation comes before the two questions it explains.
    assert output.index("Nox legt zwei Ordner an") < output.index("Datenordner [")


def test_onboard_cli_recommends_connecting_twitch_in_the_dashboard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The dashboard's code login is the recommended path, and the default answer.

    Typing a token by hand is offered only as the explicit alternative.
    """
    _, output = _run_onboard(monkeypatch, tmp_path, SKIP_EVERYTHING)
    assert "Einstellungen -> Twitch" in output and "Anmeldung per Code" in output
    assert "Stattdessen jetzt schon einen Token von Hand eintragen? [y/N]" in output


def test_onboard_cli_full_flow_stores_secrets_and_chosen_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stdin = (
        "en\n"  # interface language, asked first
        "Kumo\n"  # nox name
        "Alex\n"  # display name
        "\n"  # data folder (default)
        "\n"  # vault folder (default)
        "y\n"  # enter a Twitch token by hand instead of using the dashboard
        "oauth:zzz999\n"  # twitch oauth token
        "streambot\n"  # twitch bot user name
        "\n"  # twitch client id (skipped)
        "n\n"  # obs
        "n\n"  # telegram
        "\n"  # microphone (default yes)
        "y\n"  # camera enabled
        "y\n"  # probe the AI backends
        "ollama\n"  # explicit backend choice
    )
    exit_code, output = _run_onboard(monkeypatch, tmp_path, stdin)
    assert exit_code == 0, output

    config_path = default_user_config_path(tmp_path / "AppData" / "Roaming")
    written = load_existing_user_layer(config_path)
    assert written["identity"] == {
        "name": "Kumo",
        "user_display_name": "Alex",
        "ui_language": "en",
    }
    assert written["privacy"]["capture"] == {"microphone": True, "camera": True}
    assert written["ai"]["router"]["default_reasoner"] == "ollama"
    assert written["ai"]["providers"]["claude_code"]["enabled"] is False
    assert "oauth:zzz999" not in config_path.read_text(encoding="utf-8")
    assert "nox/twitch/oauth_token" in output
    assert "claude_code: available (ok)" in output


def test_onboard_cli_is_re_runnable_and_merges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = "\nKumo\n" + "\n" * 3 + "n\nn\nn\n" + "\n\n" + "n\n"
    _run_onboard(monkeypatch, tmp_path, first)
    second = "en\n" + "\n" * 4 + "n\nn\nn\n" + "\n\n" + "n\n"
    exit_code, output = _run_onboard(monkeypatch, tmp_path, second)
    assert exit_code == 0, output

    written = load_existing_user_layer(default_user_config_path(tmp_path / "AppData" / "Roaming"))
    assert written["identity"]["name"] == "Kumo"  # kept from the first run
    assert written["identity"]["ui_language"] == "en"  # updated by the second run
