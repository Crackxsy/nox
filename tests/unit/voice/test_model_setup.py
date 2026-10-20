"""Model downloads with progress: the byte counts, the worker's JSON lines, `nox voice setup` and
the onboarding step. Nothing here reaches the network: `httpx.stream` is replaced in every test."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from nox.voice.model_setup import (
    JsonProgress,
    configured_models_root,
    download_allowed,
    model_engine,
    model_status,
    parse_progress_line,
    run_onboarding_voice_step,
    run_voice_setup,
)
from nox.voice.models import (
    KOKORO_FILES,
    SUPERTONIC_FILES,
    DownloadProgress,
    ModelFile,
    download_files,
    files_size,
)
from nox.worker.main import main


class FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, size: int) -> Iterator[bytes]:
        yield from self.chunks


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"chunks": [b"ab", b"cd"], "urls": []}

    @contextmanager
    def fake_stream(method: str, url: str, **kwargs: Any) -> Iterator[FakeResponse]:
        state["urls"].append(url)
        yield FakeResponse(state["chunks"])

    monkeypatch.setattr(httpx, "stream", fake_stream)
    return state


def test_progress_reports_the_file_and_the_whole_run(
    tmp_path: Path, served: dict[str, Any]
) -> None:
    seen: list[DownloadProgress] = []
    files = {"a.bin": ModelFile("https://h/a", 4), "b.bin": ModelFile("https://h/b", 4)}
    assert download_files(tmp_path, files, echo=lambda _l: None, progress=seen.append) == 0
    assert [(p.file, p.file_done, p.done, p.total) for p in seen] == [
        ("a.bin", 2, 2, 8),
        ("a.bin", 4, 4, 8),
        ("b.bin", 2, 6, 8),
        ("b.bin", 4, 8, 8),
    ]
    assert seen[-1].fraction == 1.0 and (seen[-1].index, seen[-1].count) == (2, 2)


def test_present_files_do_not_count_towards_the_total(
    tmp_path: Path, served: dict[str, Any]
) -> None:
    (tmp_path / "a.bin").write_bytes(b"already")
    seen: list[DownloadProgress] = []
    files = {"a.bin": ModelFile("https://h/a", 4), "b.bin": ModelFile("https://h/b", 4)}
    assert download_files(tmp_path, files, echo=lambda _l: None, progress=seen.append) == 0
    assert {p.total for p in seen} == {4} and {p.count for p in seen} == {1}


# ---- the worker's JSON lines ---------------------------------------------------------------------


def _progress(file_done: int, file_total: int = 10) -> DownloadProgress:
    return DownloadProgress("f", file_done, file_total, file_done, file_total, 1, 1)


def test_json_progress_is_throttled_but_never_drops_a_files_last_chunk() -> None:
    lines: list[str] = []
    now = [0.0]
    out = JsonProgress(write=lines.append, min_interval_s=1.0, clock=lambda: now[0])
    out.progress(_progress(1))
    out.progress(_progress(2))  # 0 s later: dropped
    out.progress(_progress(10))  # the file's end: always sent
    out.log("done     f")
    out.result(0)
    parsed = [json.loads(line) for line in lines]
    assert [p["type"] for p in parsed] == ["progress", "progress", "log", "result"]
    assert [p.get("file_done") for p in parsed[:2]] == [1, 10]
    assert parsed[-1] == {"type": "result", "code": 0}


def test_parse_progress_line_ignores_anything_but_typed_json() -> None:
    assert parse_progress_line('{"type": "log", "text": "x"}\n') == {"type": "log", "text": "x"}
    assert parse_progress_line("fetching https://...") is None
    assert parse_progress_line("{not json") is None
    assert parse_progress_line('{"no": "type"}') is None


def test_the_worker_flag_prints_json_lines(
    tmp_path: Path, served: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--download-kokoro", "--progress-json", "--models-dir", str(tmp_path)])
    assert code == 0
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    kinds = {m["type"] for m in messages}
    assert kinds == {"log", "progress", "result"}
    assert messages[-1] == {"type": "result", "code": 0}
    assert all((tmp_path / "kokoro" / name).is_file() for name in KOKORO_FILES)
    assert len(served["urls"]) == len(KOKORO_FILES)


# ---- which files, where -------------------------------------------------------------------------


def test_the_setup_is_about_supertonic_unless_kokoro_is_configured(tmp_path: Path) -> None:
    assert model_engine("supertonic") == "supertonic"
    assert model_engine("piper") == "supertonic"
    assert model_engine("kokoro") == "kokoro"
    status = model_status("supertonic", tmp_path)
    assert status.directory == tmp_path / "supertonic"
    assert not status.present and len(status.missing) == len(SUPERTONIC_FILES)
    assert status.size_bytes == files_size(SUPERTONIC_FILES) > 390_000_000
    assert status.license == "OpenRAIL-M"


def test_models_root_follows_the_data_dir_like_the_worker_does(tmp_path: Path) -> None:
    assert configured_models_root("", tmp_path) == tmp_path / "models"
    assert configured_models_root(str(tmp_path / "m"), tmp_path) == tmp_path / "m"


def test_offline_and_private_allow_no_download() -> None:
    assert not download_allowed("offline") and not download_allowed("private")
    assert download_allowed("balanced") and download_allowed("full")


# ---- `nox voice setup` --------------------------------------------------------------------------


def _fill(directory: Path, names: Any) -> None:
    for name in names:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")


class FakeDownload:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[tuple[str, Path]] = []

    def __call__(self, engine: str, target: Path, *, echo: Any, progress: Any) -> int:
        self.calls.append((engine, target))
        total = files_size(SUPERTONIC_FILES)
        for done in (total // 4, total // 2, total):
            progress(DownloadProgress("onnx/x", done, total, done, total, 1, 1))
        return self.code


def _setup(tmp_path: Path, **kwargs: Any) -> tuple[int, list[str]]:
    out: list[str] = []
    args: dict[str, Any] = {
        "configured_engine": "supertonic",
        "root": tmp_path,
        "privacy_mode": "balanced",
        "language": "en",
        "echo": out.append,
    }
    args.update(kwargs)
    return run_voice_setup(**args), out


def test_setup_downloads_with_progress_lines_and_says_when_it_applies(tmp_path: Path) -> None:
    download = FakeDownload()
    code, out = _setup(tmp_path, download=download)
    assert code == 0
    assert download.calls == [("supertonic", tmp_path / "supertonic")]
    text = "\n".join(out)
    assert "OpenRAIL-M" in text and "AI-generated" in text
    assert sum(" %  (" in line for line in out) == 3 and "100 %" in text
    assert "next start" in text


def test_setup_with_files_present_downloads_nothing(tmp_path: Path) -> None:
    _fill(tmp_path / "supertonic", SUPERTONIC_FILES)
    download = FakeDownload()
    code, out = _setup(tmp_path, download=download)
    assert code == 0 and download.calls == []
    assert any("already there" in line for line in out)


@pytest.mark.parametrize("mode", ["offline", "private"])
def test_setup_refuses_in_offline_and_private(tmp_path: Path, mode: str) -> None:
    download = FakeDownload()
    code, out = _setup(tmp_path, privacy_mode=mode, download=download)
    assert code == 1 and download.calls == []
    assert any(mode in line for line in out)


def test_a_declined_question_downloads_nothing_and_is_no_error(tmp_path: Path) -> None:
    download = FakeDownload()
    asked: list[str] = []

    def no(question: str) -> bool:
        asked.append(question)
        return False

    code, out = _setup(tmp_path, confirm=no, download=download)
    assert code == 0 and download.calls == []
    assert "MB" in asked[0]
    assert any("nox voice setup" in line for line in out)


def test_a_failed_download_is_said_and_returns_1(tmp_path: Path) -> None:
    code, out = _setup(tmp_path, download=FakeDownload(code=1), language="de")
    assert code == 1
    assert any("fehlgeschlagen" in line and "Piper" in line for line in out)


def test_piper_has_nothing_to_download(tmp_path: Path) -> None:
    download = FakeDownload()
    code, out = _setup(tmp_path, configured_engine="piper", download=download)
    assert code == 0 and download.calls == []


# ---- the step in `nox onboard` ------------------------------------------------------------------


def _user_layer(tmp_path: Path, **voice: Any) -> Path:
    path = tmp_path / "user.yaml"
    layer = {"voice": {"models_dir": str(tmp_path / "models"), **voice}}
    path.write_text(yaml.safe_dump(layer), encoding="utf-8")
    return path


def test_onboarding_suggests_the_supertonic_download(tmp_path: Path) -> None:
    asked: list[tuple[str, bool]] = []
    download = FakeDownload()

    def ask(question: str, default: bool) -> bool:
        asked.append((question, default))
        return default

    out: list[str] = []
    code = run_onboarding_voice_step(
        "de", _user_layer(tmp_path), echo=out.append, ask=ask, download=download
    )
    assert code == 0
    assert asked and asked[0][1] is True  # default yes: Supertonic is configured and missing
    assert download.calls == [("supertonic", tmp_path / "models" / "supertonic")]
    assert any("Lizenz" in line for line in out)


def test_onboarding_asks_nothing_for_piper(tmp_path: Path) -> None:
    def ask(question: str, default: bool) -> bool:
        raise AssertionError("nothing to ask")

    path = _user_layer(tmp_path, tts={"engine": "piper"})
    assert run_onboarding_voice_step("en", path, echo=lambda _l: None, ask=ask) == 0


def test_onboarding_defaults_to_no_for_kokoro(tmp_path: Path) -> None:
    defaults: list[bool] = []

    def ask(question: str, default: bool) -> bool:
        defaults.append(default)
        return False

    path = _user_layer(tmp_path, tts={"engine": "kokoro"})
    run_onboarding_voice_step("en", path, echo=lambda _l: None, ask=ask, download=FakeDownload())
    assert defaults == [False]
