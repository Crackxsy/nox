"""The speech model arrives only through `nox voice setup`: pinned, after a yes, never alone."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from nox.voice import models
from nox.voice.model_setup import TEXTS, run_whisper_setup
from nox.voice.models import (
    WHISPER_REVISIONS,
    WHISPER_SIZES_MB,
    download_whisper,
    whisper_folder,
    whisper_installed,
)
from nox.voice.stt.faster_whisper_engine import FasterWhisperStt, WhisperModelMissingError

# ---- the engine ---------------------------------------------------------------------------------


class FakeWhisperModel:
    calls: list[dict[str, Any]] = []
    fail: Exception | None = None

    def __init__(self, source: str, **kwargs: Any) -> None:
        if FakeWhisperModel.fail is not None:
            raise FakeWhisperModel.fail
        FakeWhisperModel.calls.append({"source": source, **kwargs})


@pytest.fixture
def fake_whisper(monkeypatch: pytest.MonkeyPatch) -> type[FakeWhisperModel]:
    FakeWhisperModel.calls = []
    FakeWhisperModel.fail = None
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return FakeWhisperModel


def test_the_engine_never_asks_for_a_download(
    tmp_path: Path, fake_whisper: type[FakeWhisperModel]
) -> None:
    FasterWhisperStt(model_size="small", models_dir=str(tmp_path))._load_sync()
    assert [c["local_files_only"] for c in fake_whisper.calls] == [True]
    assert fake_whisper.calls[0]["source"] == "small"  # the cache layout under models_dir


def test_a_model_from_the_setup_is_preferred_and_also_loaded_from_disk_only(
    tmp_path: Path, fake_whisper: type[FakeWhisperModel]
) -> None:
    folder = whisper_folder(tmp_path, "base")
    folder.mkdir(parents=True)
    (folder / "model.bin").write_bytes(b"x")
    FasterWhisperStt(model_size="base", models_dir=str(tmp_path))._load_sync()
    assert fake_whisper.calls[0]["source"] == str(folder)
    assert fake_whisper.calls[0]["local_files_only"] is True


def test_a_missing_model_says_so_and_how_to_install_it(
    tmp_path: Path, fake_whisper: type[FakeWhisperModel]
) -> None:
    fake_whisper.fail = FileNotFoundError("Cannot find an appropriate cached snapshot folder")
    stt = FasterWhisperStt(model_size="small", models_dir=str(tmp_path))
    with pytest.raises(WhisperModelMissingError) as caught:
        stt._load_sync()
    message = str(caught.value)
    assert "'small'" in message and "nox voice setup" in message and "480 MB" in message
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert isinstance(caught.value, FileNotFoundError)  # what the node's own check looks for


def test_any_other_load_failure_is_not_called_a_missing_model(
    tmp_path: Path, fake_whisper: type[FakeWhisperModel]
) -> None:
    fake_whisper.fail = RuntimeError("corrupt model.bin")
    with pytest.raises(RuntimeError, match="corrupt"):
        FasterWhisperStt(model_size="small", models_dir=str(tmp_path))._load_sync()


def test_a_model_that_cannot_be_read_is_not_called_missing(
    tmp_path: Path, fake_whisper: type[FakeWhisperModel]
) -> None:
    fake_whisper.fail = PermissionError("access denied")
    with pytest.raises(PermissionError):
        FasterWhisperStt(model_size="small", models_dir=str(tmp_path))._load_sync()


async def test_a_missing_model_is_an_unavailable_engine_with_the_reason(
    tmp_path: Path, fake_whisper: type[FakeWhisperModel]
) -> None:
    fake_whisper.fail = FileNotFoundError("nothing there")
    stt = FasterWhisperStt(model_size="small", models_dir=str(tmp_path))
    with pytest.raises(WhisperModelMissingError):
        await stt.load()
    status, reason = await stt.health()
    assert str(status) == "unavailable" and "nox voice setup" in reason


# ---- the pinned download ------------------------------------------------------------------------


def test_every_pin_is_a_full_commit_hash_with_a_size() -> None:
    assert set(WHISPER_REVISIONS) == set(WHISPER_SIZES_MB) == {"tiny", "base", "small"}
    for revision in WHISPER_REVISIONS.values():
        assert len(revision) == 40 and set(revision) <= set("0123456789abcdef")


def test_the_download_is_pinned_to_the_revision_and_goes_to_the_model_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    package = types.ModuleType("faster_whisper")
    utils = types.ModuleType("faster_whisper.utils")
    utils.download_model = lambda name, **kw: seen.append((name, kw))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", package)
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)

    download_whisper("small", tmp_path)

    assert seen == [
        (
            "small",
            {
                "output_dir": str(whisper_folder(tmp_path, "small")),
                "revision": WHISPER_REVISIONS["small"],
            },
        )
    ]
    assert whisper_folder(tmp_path, "small").is_dir()


def test_a_model_without_a_pin_is_refused_before_anything_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    utils = types.ModuleType("faster_whisper.utils")
    utils.download_model = lambda *a, **kw: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    with pytest.raises(ValueError, match="no pinned revision"):
        download_whisper("large-v3-turbo", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_installed_means_the_weights_are_there_in_either_layout(tmp_path: Path) -> None:
    assert not whisper_installed(tmp_path, "small")
    whisper_folder(tmp_path, "small").mkdir()
    assert not whisper_installed(tmp_path, "small")  # a folder without weights is nothing
    (whisper_folder(tmp_path, "small") / "model.bin").write_bytes(b"x")
    assert whisper_installed(tmp_path, "small")
    assert not whisper_installed(tmp_path, "base")
    cache = tmp_path / "models--Systran--faster-whisper-base" / "snapshots" / "abc"
    cache.mkdir(parents=True)
    assert not whisper_installed(tmp_path, "base")
    (cache / "model.bin").write_bytes(b"x")
    assert whisper_installed(tmp_path, "base")


def test_the_node_uses_the_same_pins() -> None:
    from nox.node.device.voice import setup

    assert setup.WHISPER_REVISIONS is models.WHISPER_REVISIONS


# ---- `nox voice setup` --------------------------------------------------------------------------


class Recorder:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.downloads: list[tuple[str, Path]] = []
        self.fail = fail
        self.lines: list[str] = []
        self.questions: list[str] = []

    def download(self, model: str, directory: Path) -> None:
        if self.fail:
            raise self.fail
        self.downloads.append((model, directory))

    def echo(self, line: str) -> None:
        self.lines.append(line)

    def run(self, tmp_path: Path, *, answer: bool | None = True, **kw: Any) -> int:
        def confirm(question: str) -> bool:
            self.questions.append(question)
            return bool(answer)

        return run_whisper_setup(
            **{
                "model": "small",
                "directory": tmp_path,
                "privacy_mode": "balanced",
                "language": "en",
                "echo": self.echo,
                "confirm": None if answer is None else confirm,
                "download": self.download,
                **kw,
            }
        )


def test_the_model_is_downloaded_after_a_yes(tmp_path: Path) -> None:
    rec = Recorder()
    assert rec.run(tmp_path) == 0
    assert rec.downloads == [("small", tmp_path)]
    assert rec.questions == ["Download Whisper now (about 480 MB)?"]
    assert any(WHISPER_REVISIONS["small"][:10] in line for line in rec.lines)
    assert rec.lines[-1] == "Whisper is in place."


def test_a_no_downloads_nothing_and_is_not_an_error(tmp_path: Path) -> None:
    rec = Recorder()
    assert rec.run(tmp_path, answer=False) == 0
    assert rec.downloads == [] and "nox voice setup" in rec.lines[-1]


def test_the_flag_that_skips_the_question_downloads(tmp_path: Path) -> None:
    rec = Recorder()
    assert rec.run(tmp_path, answer=None) == 0
    assert rec.downloads == [("small", tmp_path)] and rec.questions == []


@pytest.mark.parametrize("mode", ["offline", "private"])
def test_the_private_modes_download_nothing_and_do_not_even_ask(tmp_path: Path, mode: str) -> None:
    rec = Recorder()
    assert rec.run(tmp_path, privacy_mode=mode) == 1
    assert rec.downloads == [] and rec.questions == []
    assert mode in rec.lines[-1]


@pytest.mark.parametrize("mode", ["full", "balanced", "minimal"])
def test_the_other_modes_may_download(tmp_path: Path, mode: str) -> None:
    rec = Recorder()
    assert rec.run(tmp_path, privacy_mode=mode) == 0
    assert len(rec.downloads) == 1


def test_a_model_that_is_already_there_is_left_alone(tmp_path: Path) -> None:
    whisper_folder(tmp_path, "small").mkdir()
    (whisper_folder(tmp_path, "small") / "model.bin").write_bytes(b"x")
    rec = Recorder()
    assert rec.run(tmp_path) == 0
    assert rec.downloads == [] and rec.questions == []
    assert "already there" in rec.lines[-1]


def test_a_model_without_a_pin_is_not_fetched(tmp_path: Path) -> None:
    rec = Recorder()
    assert rec.run(tmp_path, model="large-v3-turbo") == 0
    assert rec.downloads == [] and rec.questions == []
    assert "no pinned revision" in rec.lines[-1]


def test_a_failed_download_is_reported_not_raised(tmp_path: Path) -> None:
    rec = Recorder(fail=OSError("connection reset"))
    assert rec.run(tmp_path) == 1
    assert "OSError: connection reset" in rec.lines[-1]


def test_the_german_texts_exist_for_every_english_one() -> None:
    assert set(TEXTS["de"]) == set(TEXTS["en"])
    assert TEXTS["de"]["whisper_ask"].startswith("Whisper jetzt")
