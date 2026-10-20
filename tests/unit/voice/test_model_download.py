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
from nox.voice.models import (
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
    (tmp_path / "a.bin").write_bytes(b"already here")
    out, echo = lines()
    files = {"a.bin": ModelFile("https://h/a", len(PAYLOAD), DIGEST)}
    assert download_files(tmp_path, files, echo=echo) == 0
    assert served["urls"] == []
    assert download_files(tmp_path, files, echo=echo, force=True) == 0
    assert (tmp_path / "a.bin").read_bytes() == PAYLOAD


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
