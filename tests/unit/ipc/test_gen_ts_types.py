"""scripts/gen_ts_types.py: deterministic, idempotent, covers ChatStreamFrame/ChatSendResult."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "gen_ts_types.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_ts_types", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen_ts_types = _load_module()


def test_generate_is_deterministic_and_idempotent() -> None:
    first = gen_ts_types.generate()
    second = gen_ts_types.generate()
    assert first == second
    assert first.startswith("/**")
    assert first.endswith("\n")


def test_covers_chat_stream_frame_and_result() -> None:
    out = gen_ts_types.generate()
    assert "export interface ChatStreamFrame {" in out
    assert "delta: string;" in out
    assert "done?: boolean;" in out
    assert "export interface ChatSendResult {" in out
    assert "request_id: string;" in out
    assert "text: string;" in out
    assert "provider: string;" in out
    assert "degraded: boolean;" in out


def test_covers_envelope_and_shared_enums() -> None:
    out = gen_ts_types.generate()
    assert 'export type Kind = "event" | "request" | "response" | "error" | "stream";' in out
    assert "export interface Envelope {" in out
    assert "export interface Source {" in out
    assert "export interface AuthRequest {" in out
    assert "export interface AuthResponse {" in out
    assert "export interface ErrorPayload {" in out


def test_covers_event_payload_models() -> None:
    out = gen_ts_types.generate()
    assert "export interface HealthReport {" in out
    assert "components: Record<string, HealthChanged>;" in out
    assert "export interface PetStateChanged {" in out


def test_optional_and_nullable_fields() -> None:
    out = gen_ts_types.generate()
    # StateChanged.old/new: `Any = None` -> no JSON Schema type -> unknown, optional (no default
    # required entry).
    assert "old?: unknown;" in out
    assert "new?: unknown;" in out
    # AiResponseReady.tokens_in: `int | None = None` -> nullable AND optional.
    assert "tokens_in?: number | null;" in out


def test_check_mode_passes_against_the_committed_file() -> None:
    """The committed `ui/shared/generated/ipc.ts` must already match the current models: run
    `python scripts/gen_ts_types.py` and commit the result whenever a payload model changes.
    """
    assert gen_ts_types.OUTPUT_PATH.exists(), (
        "ui/shared/generated/ipc.ts is missing; run `python scripts/gen_ts_types.py`"
    )
    committed = gen_ts_types.OUTPUT_PATH.read_text(encoding="utf-8")
    assert committed == gen_ts_types.generate()


def test_check_returns_1_when_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = tmp_path / "ipc.ts"
    stale.write_text("stale content", encoding="utf-8")
    monkeypatch.setattr(gen_ts_types, "OUTPUT_PATH", stale)
    assert gen_ts_types.main(["--check"]) == 1
    assert gen_ts_types.main([]) == 0
    assert stale.read_text(encoding="utf-8") == gen_ts_types.generate()
    assert gen_ts_types.main(["--check"]) == 0
