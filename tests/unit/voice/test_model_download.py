"""User-initiated model downloads: pinned hashes, atomic writes, no half-written file left."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from nox.voice import models
from nox.voice.model_setup import privacy_cancel
from nox.voice.models import (
    KOKORO_FILES,
    KOKORO_REVISION,
    SUPERTONIC_FILES,
    SUPERTONIC_REQUIRED,
    SUPERTONIC_REVISION,
    SUPERTONIC_VOICES,
    ModelFile,
    download_files,
    download_supertonic,
)

PAYLOAD = b"model bytes"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("GET", "https://x"), response=httpx.Response(404)
            )

    def iter_bytes(self, size: int) -> Iterator[bytes]:
        yield self.body


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every URL answers with `served["body"]`; requested URLs are recorded."""
    state: dict[str, Any] = {"body": PAYLOAD, "status": 200, "urls": []}

    @contextmanager
    def fake_stream(method: str, url: str, **kwargs: Any) -> Iterator[FakeResponse]:
        state["urls"].append(url)
        yield FakeResponse(state["body"], state["status"])

    monkeypatch.setattr(httpx, "stream", fake_stream)
    return state


def lines() -> tuple[list[str], Any]:
    out: list[str] = []
    return out, out.append


def test_a_pinned_file_is_verified_and_lands_in_its_subfolder(
    tmp_path: Path, served: dict[str, Any]
) -> None:
    out, echo = lines()
    files = {"onnx/a.onnx": ModelFile("https://h/a", len(PAYLOAD), DIGEST)}
    assert download_files(tmp_path, files, echo=echo) == 0
    assert (tmp_path / "onnx" / "a.onnx").read_bytes() == PAYLOAD
    assert not list(tmp_path.rglob("*.part"))


def test_a_hash_mismatch_discards_the_file_and_fails(
    tmp_path: Path, served: dict[str, Any]
) -> None:
    served["body"] = b"tampered"
    out, echo = lines()
    files = {"onnx/a.onnx": ModelFile("https://h/a", len(PAYLOAD), DIGEST)}
    assert download_files(tmp_path, files, echo=echo) == 1
    assert not (tmp_path / "onnx" / "a.onnx").exists()
    assert not list(tmp_path.rglob("*.part"))
    assert any("SHA-256" in line for line in out)


def test_an_http_error_leaves_nothing_behind(tmp_path: Path, served: dict[str, Any]) -> None:
    served["status"] = 404
    out, echo = lines()
    assert download_files(tmp_path, {"a.bin": ModelFile("https://h/a", 1)}, echo=echo) == 1
    assert list(tmp_path.iterdir()) == []


def test_existing_files_are_kept_unless_forced(tmp_path: Path, served: dict[str, Any]) -> None:
    (tmp_path / "a.bin").write_bytes(PAYLOAD)
    out, echo = lines()
    files = {"a.bin": ModelFile("https://h/a", len(PAYLOAD), DIGEST)}
    assert download_files(tmp_path, files, echo=echo) == 0
    assert served["urls"] == []
    assert any(line.startswith("exists") for line in out)
    assert download_files(tmp_path, files, echo=echo, force=True) == 0
    assert served["urls"] == ["https://h/a"]
    assert (tmp_path / "a.bin").read_bytes() == PAYLOAD


def test_an_existing_file_with_the_wrong_hash_is_detected_and_replaced(
    tmp_path: Path, served: dict[str, Any]
) -> None:
    # Same size as the pinned file, different bytes: only the SHA-256 can tell.
    (tmp_path / "a.bin").write_bytes(b"x" * len(PAYLOAD))
    out, echo = lines()
    files = {"a.bin": ModelFile("https://h/a", len(PAYLOAD), DIGEST)}
    assert download_files(tmp_path, files, echo=echo) == 0
    assert any(line.startswith("mismatch") for line in out)
    assert served["urls"] == ["https://h/a"]
    assert (tmp_path / "a.bin").read_bytes() == PAYLOAD


def test_a_wrong_existing_file_is_never_accepted_when_the_refetch_is_bad_too(
    tmp_path: Path, served: dict[str, Any]
) -> None:
    (tmp_path / "a.bin").write_bytes(b"x" * len(PAYLOAD))
    served["body"] = b"y" * len(PAYLOAD)  # the server hands out a tampered copy as well
    out, echo = lines()
    files = {"a.bin": ModelFile("https://h/a", len(PAYLOAD), DIGEST)}
    assert download_files(tmp_path, files, echo=echo) == 1
    assert any(line.startswith("mismatch") for line in out)
    assert any("SHA-256" in line for line in out)
    assert not models.file_matches(tmp_path / "a.bin", files["a.bin"])
    assert not list(tmp_path.rglob("*.part"))


def test_an_oversized_stream_is_cut_off_and_nothing_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunk = b"z" * 1024
    sent: list[int] = []

    class Endless(FakeResponse):
        def iter_bytes(self, size: int) -> Iterator[bytes]:
            while True:  # a server that never stops sending
                sent.append(len(chunk))
                yield chunk

    @contextmanager
    def fake_stream(method: str, url: str, **kwargs: Any) -> Iterator[FakeResponse]:
        yield Endless(b"")

    monkeypatch.setattr(httpx, "stream", fake_stream)
    out, echo = lines()
    files = {"a.bin": ModelFile("https://h/a", 4096, DIGEST)}
    assert download_files(tmp_path, files, echo=echo) == 1
    assert sum(sent) <= 4096 + models.SIZE_MARGIN_BYTES + len(chunk)
    assert any("exceeds" in line for line in out)
    assert list(tmp_path.iterdir()) == []


def test_an_announced_oversize_is_refused_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[bool] = []

    class Huge(FakeResponse):
        headers = {"content-length": str(10**12)}

        def iter_bytes(self, size: int) -> Iterator[bytes]:
            read.append(True)
            yield b""

    @contextmanager
    def fake_stream(method: str, url: str, **kwargs: Any) -> Iterator[FakeResponse]:
        yield Huge(b"")

    monkeypatch.setattr(httpx, "stream", fake_stream)
    out, echo = lines()
    files = {"a.bin": ModelFile("https://h/a", len(PAYLOAD), DIGEST)}
    assert download_files(tmp_path, files, echo=echo) == 1
    assert read == []
    assert list(tmp_path.iterdir()) == []


def test_a_short_stream_is_rejected(tmp_path: Path, served: dict[str, Any]) -> None:
    served["body"] = PAYLOAD[:-1]
    out, echo = lines()
    files = {"a.bin": ModelFile("https://h/a", len(PAYLOAD))}  # no pin: the size still counts
    assert download_files(tmp_path, files, echo=echo) == 1
    assert list(tmp_path.iterdir()) == []


def test_a_privacy_switch_stops_a_running_download_and_keeps_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TwoChunks(FakeResponse):
        def iter_bytes(self, size: int) -> Iterator[bytes]:
            yield PAYLOAD[:4]
            yield PAYLOAD[4:]

    @contextmanager
    def fake_stream(method: str, url: str, **kwargs: Any) -> Iterator[FakeResponse]:
        yield TwoChunks(b"")

    monkeypatch.setattr(httpx, "stream", fake_stream)
    mode = ["balanced"]
    seen: list[int] = []

    def progress(p: models.DownloadProgress) -> None:
        seen.append(p.file_done)
        mode[0] = "offline"  # the user switches privacy while the first chunk is being written

    out, echo = lines()
    files = {
        "a.bin": ModelFile("https://h/a", len(PAYLOAD), DIGEST),
        "b.bin": ModelFile("https://h/b", len(PAYLOAD), DIGEST),
    }
    cancel = privacy_cancel(lambda: mode[0])
    code = download_files(tmp_path, files, echo=echo, progress=progress, cancel=cancel)
    assert code == models.EXIT_ABORTED
    assert seen == [4]  # stopped after the first chunk; the second file never started
    assert any(line.startswith("aborted") and "offline" in line for line in out)
    assert list(tmp_path.iterdir()) == []


def test_a_blocking_privacy_mode_fetches_nothing(tmp_path: Path, served: dict[str, Any]) -> None:
    out, echo = lines()
    files = {"a.bin": ModelFile("https://h/a", len(PAYLOAD), DIGEST)}
    code = download_files(tmp_path, files, echo=echo, cancel=privacy_cancel(lambda: "private"))
    assert code == models.EXIT_ABORTED
    assert served["urls"] == []


def test_kokoro_files_are_pinned_to_one_commit_with_hashes() -> None:
    assert len(KOKORO_REVISION) == 40
    assert set(KOKORO_FILES) == {"kokoro-v1.0.onnx", "voices-v1.0.bin"}
    for name, source in KOKORO_FILES.items():
        assert source.url == (
            f"https://huggingface.co/fastrtc/kokoro-onnx/resolve/{KOKORO_REVISION}/{name}"
        )
        assert len(source.sha256) == 64
        assert source.size > 0
    assert KOKORO_FILES["kokoro-v1.0.onnx"].sha256.startswith("7d5df8ec")
    assert KOKORO_FILES["voices-v1.0.bin"].sha256.startswith("bca610b8")
    assert models.kokoro_files() == KOKORO_FILES


def test_supertonic_files_are_pinned_to_one_commit_with_hashes() -> None:
    assert len(SUPERTONIC_REVISION) == 40
    for name, source in SUPERTONIC_FILES.items():
        assert f"/resolve/{SUPERTONIC_REVISION}/{name}" in source.url
        assert source.url.startswith("https://huggingface.co/Supertone/supertonic-3/")
        assert len(source.sha256) == 64 and source.size > 0
    assert "LICENSE" in SUPERTONIC_FILES  # the OpenRAIL-M terms travel with the weights
    assert set(SUPERTONIC_REQUIRED) <= set(SUPERTONIC_FILES)
    assert {"F1", "F3", "M1", "M5"} <= set(SUPERTONIC_VOICES)


def test_download_supertonic_names_the_license_and_the_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, ModelFile]] = []

    def fake_download(target: Path, files: dict[str, ModelFile], **kwargs: Any) -> int:
        calls.append(files)
        return 0

    monkeypatch.setattr(models, "download_files", fake_download)
    out, echo = lines()
    assert download_supertonic(tmp_path, echo=echo) == 0
    assert calls == [SUPERTONIC_FILES]
    text = "\n".join(out)
    assert "OpenRAIL-M" in text
    assert "voice.tts.engine: supertonic" in text
