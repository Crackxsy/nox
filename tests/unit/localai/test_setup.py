"""The setup flow against a fake Ollama: detect, recommend, pull, probe, write, and each refusal."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nox.ai.config import OllamaConfig
from nox.ai.local_models import Hardware
from nox.core.state import PrivacyMode
from nox.localai.ollama_setup import InstallResult, OllamaInstaller
from nox.localai.setup import Outcome, Question, SetupDeps, SetupState, run_setup
from tests.unit.localai.fake_ollama import FakeOllama, prose

GTX_1060 = Hardware(ram_gb=32.0, vram_gb=6.0, machine="AMD64")
AMD_DESKTOP = Hardware(ram_gb=16.0, vram_gb=None, machine="AMD64")
PI_4 = Hardware(ram_gb=8.0, vram_gb=None, machine="aarch64", pi_generation=4)
MODEL = "qwen3:4b-instruct"


def _deps(
    fake: FakeOllama,
    tmp_path: Path,
    *,
    hardware: Hardware = GTX_1060,
    privacy: PrivacyMode = PrivacyMode.BALANCED,
    installer: OllamaInstaller | None = None,
    binary: str | None = None,
) -> SetupDeps:
    return SetupDeps(
        ollama=OllamaConfig(),
        client_factory=fake.factory(),
        privacy_mode=lambda: privacy,
        user_config_path=tmp_path / "user.yaml",
        detect=lambda: hardware,
        installer=lambda: installer,
        find=lambda _name: binary,
        system="Windows",
    )


async def _yes(_question: Question) -> bool:
    return True


async def _no(_question: Question) -> bool:
    return False


async def test_a_full_run_pulls_probes_and_writes_the_model(tmp_path: Path) -> None:
    (tmp_path / "user.yaml").write_text("# mine\nidentity:\n  name: Nox  # keep\n", "utf-8")
    fake = FakeOllama()
    seen: list[int] = []
    state = SetupState()
    state.listeners.append(lambda s: seen.append(s.pull.completed if s.pull else -1))

    result = await run_setup(_deps(fake, tmp_path), confirm=_yes, state=state)

    assert result.outcome is Outcome.DONE, result.detail
    assert fake.pull_bodies == [{"model": MODEL, "stream": True}]
    assert 500 in seen and 1000 in seen, "progress reached the listener"
    text = (tmp_path / "user.yaml").read_text("utf-8")
    assert yaml.safe_load(text)["ai"]["providers"]["ollama"]["model"] == MODEL
    assert "# mine" in text and "# keep" in text, "the round-trip keeps comments"
    assert result.model_written == MODEL
    tool_body = fake.chat_bodies[-1]
    assert tool_body["model"] == MODEL
    assert tool_body["tools"][0]["function"]["name"] == "desktop-windows"


@pytest.mark.parametrize("mode", [PrivacyMode.OFFLINE, PrivacyMode.PRIVATE])
async def test_no_pull_in_offline_or_private(tmp_path: Path, mode: PrivacyMode) -> None:
    fake = FakeOllama()

    result = await run_setup(_deps(fake, tmp_path, privacy=mode), confirm=_yes)

    assert result.outcome is Outcome.PRIVACY_BLOCKED
    assert "/api/pull" not in fake.paths()
    assert not (tmp_path / "user.yaml").exists()
    assert result.to_payload()["pull_blocked"] is True


async def test_offline_still_sets_up_a_model_that_is_already_there(tmp_path: Path) -> None:
    fake = FakeOllama(models=[MODEL])

    result = await run_setup(_deps(fake, tmp_path, privacy=PrivacyMode.OFFLINE), confirm=_yes)

    assert result.outcome is Outcome.DONE
    assert "/api/pull" not in fake.paths()


async def test_a_failed_tool_probe_writes_nothing(tmp_path: Path) -> None:
    fake = FakeOllama(tool_answer=prose("Ich sehe gerade drei Fenster."))

    result = await run_setup(_deps(fake, tmp_path), confirm=_yes)

    assert result.outcome is Outcome.PROBE_FAILED
    assert result.probe is not None and result.probe.text_ok and not result.probe.tool_ok
    assert not (tmp_path / "user.yaml").exists()


async def test_a_model_without_native_tools_fails_the_probe(tmp_path: Path) -> None:
    (tmp_path / "user.yaml").write_text(
        "ai:\n  providers:\n    ollama:\n      model: old\n", "utf-8"
    )
    fake = FakeOllama(tools_capability=False)

    result = await run_setup(_deps(fake, tmp_path), confirm=_yes)

    assert result.outcome is Outcome.PROBE_FAILED
    assert "native tool calling" in result.detail
    loaded = yaml.safe_load((tmp_path / "user.yaml").read_text("utf-8"))
    assert loaded["ai"]["providers"]["ollama"]["model"] == "old", "user.yaml untouched"


async def test_a_raspberry_pi_4_is_told_no_and_nothing_is_asked(tmp_path: Path) -> None:
    fake = FakeOllama()

    result = await run_setup(_deps(fake, tmp_path, hardware=PI_4), confirm=_yes)

    assert result.outcome is Outcome.NO_LLM
    assert fake.requests == [], "no model means no reason to talk to Ollama at all"
    assert result.to_payload()["recommendation"] == {
        "model": None,
        "reason": "no_llm_pi",
        "basis": None,
        "download_gb": None,
        "measured": None,
    }


async def test_without_a_vram_reading_the_payload_says_it_chose_by_ram(tmp_path: Path) -> None:
    result = await run_setup(
        _deps(FakeOllama(), tmp_path, hardware=AMD_DESKTOP), confirm=_yes, dry_run=True
    )

    payload = result.to_payload()
    assert payload["recommendation"]["basis"] == "ram"
    assert payload["recommendation"]["reason"] == "ram_no_vram_reading"
    assert payload["hardware"]["vram_gb"] is None


async def test_a_dry_run_changes_nothing(tmp_path: Path) -> None:
    fake = FakeOllama()

    result = await run_setup(_deps(fake, tmp_path), confirm=_yes, dry_run=True)

    assert result.outcome is Outcome.RECOMMENDED
    assert set(fake.paths()) == {"/api/version", "/api/tags"}
    assert not (tmp_path / "user.yaml").exists()


async def test_declining_the_download_stops_before_it(tmp_path: Path) -> None:
    fake = FakeOllama()

    result = await run_setup(_deps(fake, tmp_path), confirm=_no)

    assert result.outcome is Outcome.DECLINED
    assert "/api/pull" not in fake.paths()


async def test_a_failed_download_is_reported_in_ollamas_words(tmp_path: Path) -> None:
    fake = FakeOllama(pull=[{"status": "pulling manifest"}, {"error": "pull model manifest: 404"}])

    result = await run_setup(_deps(fake, tmp_path), confirm=_yes)

    assert result.outcome is Outcome.PULL_FAILED
    assert "manifest: 404" in result.detail
    assert not (tmp_path / "user.yaml").exists()


async def test_missing_ollama_without_an_installer_gives_instructions(tmp_path: Path) -> None:
    fake = FakeOllama(reachable=False)

    result = await run_setup(_deps(fake, tmp_path), confirm=_yes)

    assert result.outcome is Outcome.OLLAMA_MISSING
    assert any("winget" in line for line in result.instructions)
    assert result.to_payload()["ollama"]["can_install"] is False


async def test_installed_but_stopped_ollama_is_not_installed_again(tmp_path: Path) -> None:
    calls: list[str] = []

    async def installer() -> InstallResult:
        calls.append("install")
        return InstallResult(ok=True)

    fake = FakeOllama(reachable=False)
    deps = _deps(fake, tmp_path, installer=installer, binary="/usr/bin/ollama")

    result = await run_setup(deps, confirm=_yes)

    assert result.outcome is Outcome.OLLAMA_NOT_RUNNING
    assert calls == []


async def test_missing_ollama_is_installed_through_the_installer_when_confirmed(
    tmp_path: Path,
) -> None:
    fake = FakeOllama(reachable=False)
    asked: list[Question] = []

    async def installer() -> InstallResult:
        fake.reachable = True
        return InstallResult(ok=True)

    async def confirm(question: Question) -> bool:
        asked.append(question)
        return True

    result = await run_setup(_deps(fake, tmp_path, installer=installer), confirm=confirm)

    assert asked == [Question.INSTALL_OLLAMA, Question.PULL]
    assert result.outcome is Outcome.DONE


async def test_a_refused_install_ends_honestly(tmp_path: Path) -> None:
    async def installer() -> InstallResult:
        return InstallResult(ok=False, detail="permission_denied")

    result = await run_setup(
        _deps(FakeOllama(reachable=False), tmp_path, installer=installer), confirm=_yes
    )

    assert result.outcome is Outcome.INSTALL_FAILED
    assert result.detail == "permission_denied"
