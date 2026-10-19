"""`nox ai setup` in a terminal: what it prints and which exit code it ends with."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from nox.ai.local_models import Hardware
from nox.cli import app
from nox.core.state import PrivacyMode
from nox.localai import cli as localai_cli
from nox.localai.cli import run_local_ai_setup
from tests.unit.localai.fake_ollama import FakeOllama
from tests.unit.localai.test_setup import AMD_DESKTOP, PI_4, _deps


class _Asked(list[str]):
    """Records every question and answers no."""

    def __call__(self, text: str) -> bool:
        self.append(text)
        return False


async def _run(deps: object, **kwargs: object) -> tuple[int, str]:
    lines: list[str] = []
    code = await run_local_ai_setup(deps=deps, echo=lines.append, **kwargs)  # type: ignore[arg-type]
    return code, "\n".join(lines)


async def test_no_vram_reading_says_it_chose_by_ram_in_german(tmp_path: Path) -> None:
    deps = _deps(FakeOllama(), tmp_path, hardware=AMD_DESKTOP)
    code, out = await _run(deps, dry_run=True, language="de")
    assert code == 0
    assert "Kein Grafikspeicher-Messwert" in out
    assert "gewählt nach Arbeitsspeicher" in out
    assert "nichts wurde verändert" in out


async def test_no_vram_reading_says_it_chose_by_ram_in_english(tmp_path: Path) -> None:
    deps = _deps(FakeOllama(), tmp_path, hardware=AMD_DESKTOP)
    code, out = await _run(deps, dry_run=True, language="en")
    assert code == 0
    assert "chosen by RAM" in out
    assert "VRAM: no reading" in out


async def test_a_pi_4_is_told_honestly_that_no_local_model_makes_sense(tmp_path: Path) -> None:
    code, out = await _run(_deps(FakeOllama(), tmp_path, hardware=PI_4), language="en")
    assert code == 0
    assert "Raspberry Pi 4 or older" in out
    assert "No local model will be set up." in out


async def test_yes_runs_without_asking_and_shows_progress(tmp_path: Path) -> None:
    asked = _Asked()
    code, out = await _run(_deps(FakeOllama(), tmp_path), assume_yes=True, language="en", ask=asked)
    assert code == 0, out
    assert asked == []
    assert "Downloading qwen3:4b-instruct" in out
    assert "50%" in out and "100%" in out
    assert "Probing the model" in out
    assert "Done: qwen3:4b-instruct" in out


async def test_without_yes_the_download_is_asked_and_a_no_is_respected(tmp_path: Path) -> None:
    fake = FakeOllama()
    asked = _Asked()
    code, out = await _run(_deps(fake, tmp_path), language="en", ask=asked)
    assert code == 0
    assert asked == ["Download qwen3:4b-instruct now (about 2.5 GB)?"]
    assert "/api/pull" not in fake.paths()


async def test_offline_refuses_the_download_with_exit_code_1(tmp_path: Path) -> None:
    deps = _deps(FakeOllama(), tmp_path, privacy=PrivacyMode.OFFLINE)
    code, out = await _run(deps, assume_yes=True, language="en")
    assert code == 1
    assert "privacy mode offline" in out


async def test_missing_ollama_prints_the_install_command(tmp_path: Path) -> None:
    code, out = await _run(_deps(FakeOllama(reachable=False), tmp_path), language="en")
    assert code == 1
    assert "winget install --id Ollama.Ollama -e" in out


def test_the_command_is_wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOllama()
    monkeypatch.setattr(
        localai_cli,
        "build_cli_deps",
        lambda _config, _path: _deps(
            fake, tmp_path, hardware=Hardware(ram_gb=32, vram_gb=6, machine="AMD64")
        ),
    )
    result = CliRunner().invoke(app, ["ai", "setup", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "qwen3:4b-instruct" in result.output
    assert "/api/pull" not in fake.paths()
