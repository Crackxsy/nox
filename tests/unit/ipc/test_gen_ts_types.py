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
    monkeypatch.setattr(gen_ts_types, "VERSION_OUTPUT_PATH", tmp_path / "version.ts")
    assert gen_ts_types.main(["--check"]) == 1
    assert gen_ts_types.main([]) == 0
    assert stale.read_text(encoding="utf-8") == gen_ts_types.generate()
    assert gen_ts_types.main(["--check"]) == 0


# ---- the version both UIs send -----------------------------------------------------------------


def test_the_ui_client_version_is_generated_from_the_package_version() -> None:
    """A hand-kept '0.1.0' in the UIs would one day be refused by a 1.x core (major check)."""
    import nox

    version = gen_ts_types.package_version()
    assert version == nox.__version__  # one version number for the whole product
    assert gen_ts_types.VERSION_OUTPUT_PATH.read_text(encoding="utf-8") == (
        gen_ts_types.generate_version()
    )
    assert f'export const NOX_VERSION = "{version}";' in gen_ts_types.generate_version()


def test_the_hub_accepts_the_generated_client_version() -> None:
    from nox.ipc.server import major_version

    assert major_version(gen_ts_types.package_version()) == major_version(
        __import__("nox").__version__
    )


def test_no_ui_hand_writes_a_client_version() -> None:
    """Both clients import `NOX_VERSION`; a literal version string in them is the old bug."""
    for path in (
        REPO_ROOT / "ui" / "dashboard" / "src" / "ipc.ts",
        REPO_ROOT / "ui" / "pet" / "src" / "ipc.ts",
        REPO_ROOT / "ui" / "shared" / "ipc.ts",
    ):
        text = path.read_text(encoding="utf-8")
        assert "'0.1.0'" not in text and '"0.1.0"' not in text, path
    for path in (
        REPO_ROOT / "ui" / "dashboard" / "src" / "ipc.ts",
        REPO_ROOT / "ui" / "pet" / "src" / "ipc.ts",
    ):
        assert "clientVersion: NOX_VERSION" in path.read_text(encoding="utf-8"), path


def test_check_fails_when_the_version_file_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "version.ts"
    stale.write_text('export const NOX_VERSION = "0.1.0";\n', encoding="utf-8")
    monkeypatch.setattr(gen_ts_types, "VERSION_OUTPUT_PATH", stale)
    assert gen_ts_types.main(["--check"]) == 1
