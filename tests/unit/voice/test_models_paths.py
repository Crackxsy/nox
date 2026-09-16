"""Where voice models are looked for, and how the worker wires the configuration to the engines."""

from __future__ import annotations

from pathlib import Path

import pytest

from nox.core.config import VoiceConfig
from nox.voice.models import (
    KOKORO_FILES,
    default_clips_dir,
    default_models_root,
    engine_models_dir,
    missing_files,
    models_root,
)
from nox.worker.main import build_components, build_wake_gate, pipeline_config_from


def test_configured_models_dir_wins(tmp_path: Path) -> None:
    assert models_root(tmp_path / "elsewhere") == tmp_path / "elsewhere"
    assert engine_models_dir("kokoro", tmp_path) == tmp_path / "kokoro"


def test_models_root_follows_the_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NOX_DATA_DIR", str(tmp_path / "data"))
    assert default_models_root() == tmp_path / "data" / "models"
    assert default_clips_dir() == tmp_path / "data" / "data" / "clips"
    assert engine_models_dir("openwakeword") == tmp_path / "data" / "models" / "openwakeword"


def test_models_root_falls_back_to_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NOX_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_models_root() == tmp_path / "Nox" / "models"


def test_no_absolute_machine_path_is_baked_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard for the public repository: no developer path in the defaults."""
    monkeypatch.delenv("NOX_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")
    assert "Nox" in str(default_models_root())
    assert str(default_models_root()).startswith(r"C:\Users\someone")


def test_missing_files_lists_what_is_absent(tmp_path: Path) -> None:
    names = tuple(KOKORO_FILES)
    assert missing_files(tmp_path, names) == list(names)
    (tmp_path / names[0]).write_bytes(b"x")
    assert missing_files(tmp_path, names) == [names[1]]


# ---- worker wiring -------------------------------------------------------------------------------


def test_build_components_selects_the_configured_engine(tmp_path: Path) -> None:
    cfg = VoiceConfig.model_validate({"models_dir": str(tmp_path), "tts": {"engine": "kokoro"}})
    components = build_components(cfg, service="tts")
    tts = components["tts"]
    assert tts.id == "kokoro"
    assert tts.models_dir == tmp_path / "kokoro"


def test_build_components_defaults_to_piper(tmp_path: Path) -> None:
    cfg = VoiceConfig.model_validate({"models_dir": str(tmp_path)})
    components = build_components(cfg, service="tts")
    assert components["tts"].id == "piper"
    assert components["tts"].models_dir == tmp_path / "piper"


def test_stt_models_follow_the_models_dir(tmp_path: Path) -> None:
    cfg = VoiceConfig.model_validate({"models_dir": str(tmp_path)})
    components = build_components(cfg, service="stt")
    assert Path(components["stt"].models_dir) == tmp_path / "faster-whisper"


def test_explicit_voice_overrides_the_kokoro_default(tmp_path: Path) -> None:
    cfg = VoiceConfig.model_validate(
        {"models_dir": str(tmp_path), "tts": {"engine": "kokoro", "voice": "am_michael"}}
    )
    tts = build_components(cfg, service="tts")["tts"]
    assert set(tts.voice_names.values()) == {"am_michael"}


def test_listening_mode_reaches_the_pipeline_config() -> None:
    cfg = VoiceConfig.model_validate({"stt": {"listening_mode": "ptt_only"}})
    assert pipeline_config_from(cfg).listening_mode == "ptt_only"


def test_wake_gate_falls_back_to_text_without_a_model(tmp_path: Path) -> None:
    cfg = VoiceConfig.model_validate({"models_dir": str(tmp_path)})
    gate = build_wake_gate(cfg)
    assert gate.acoustic is False
    assert gate.config.wake_window_s == cfg.stt.wake_window_s
    assert gate.config.kill_watchdog_max_ms == cfg.stt.kill_watchdog_max_ms


def test_wake_gate_config_is_taken_from_the_voice_section(tmp_path: Path) -> None:
    cfg = VoiceConfig.model_validate(
        {
            "models_dir": str(tmp_path),
            "stt": {
                "wake_word_engine": "text",
                "wake_word_threshold": 0.9,
                "wake_window_s": 3.0,
                "conversation_window_s": 5.0,
                "kill_phrase_watchdog": False,
            },
        }
    )
    gate = build_wake_gate(cfg)
    assert gate.config.threshold == 0.9
    assert gate.config.wake_window_s == 3.0
    assert gate.config.conversation_window_s == 5.0
    assert gate.config.kill_watchdog is False
