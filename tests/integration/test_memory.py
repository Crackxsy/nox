"""Integration: boot the real core headless with a tmp vault, install EPIC-07 Memory & Vault,
write a note through the `vault.append_inbox` tool, confirm it is indexed and found by
`memory.search`, and confirm PRIVATE privacy mode blocks `memory.write` - all through
`core.tool_executor.call()`, the same pipeline every agent/tool call goes through."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.state import PrivacyMode
from nox.memory.install import install as install_memory
from tests._ports import free_port_base


def _config(tmp_path: Path):
    base = free_port_base()
    overrides = {
        "paths": {
            "data_dir": str(tmp_path / "data"),
            "vault_dir": str(tmp_path / "vault"),
            "index_dir": str(tmp_path / "index"),
            "database_dir": str(tmp_path / "db"),
            "cache_dir": str(tmp_path / "cache"),
            "backups_dir": str(tmp_path / "backups"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "privacy": {"mode": "balanced"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
    }
    (tmp_path / "vault").mkdir()
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
async def core(tmp_path: Path):
    cfg = _config(tmp_path)
    nox_core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await asyncio.wait_for(nox_core.start(), timeout=60)
    nox_core.extensions["memory"] = install_memory(nox_core)
    try:
        yield nox_core
    finally:
        await asyncio.wait_for(nox_core.stop(), timeout=30)


async def test_write_index_and_search_round_trip(core: NoxCore) -> None:
    # `vault.append_inbox`/`memory.write` are pre-allowed for the "companion" profile (see
    # config/profiles/companion.yaml `companion.vault.inbox`/`companion.memory.write` rules) - no
    # confirmation round-trip needed, unlike e.g. `pm.story.update_status` in test_pm.py.
    write_result = await core.tool_executor.call(
        "nox.chat",
        "vault.append_inbox",
        {
            "title": "Kill Switch Note",
            "body": "The kill switch latency target is 100ms.",
            "source": "test-session",
        },
        mode="companion",
    )
    assert write_result.ok, write_result.error
    assert write_result.data is not None
    assert write_result.data["ok"] is True

    search_result = await core.tool_executor.call(
        "nox.chat", "memory.search", {"query": "kill switch latency", "k": 5}, mode="companion"
    )
    assert search_result.ok, search_result.error
    assert search_result.data is not None
    assert search_result.data["found"] is True
    assert any("kill switch" in item["text"].lower() for item in search_result.data["items"])


async def test_memory_write_tool_succeeds_in_balanced_mode(core: NoxCore) -> None:
    result = await core.tool_executor.call(
        "nox.chat",
        "memory.write",
        {"text": "merk dir: Lieblingsfarbe ist blau", "source": "test"},
        mode="companion",
    )
    assert result.ok, result.error
    assert result.data is not None
    assert result.data["ok"] is True
    assert result.data["importance"] == 1.0


async def test_privacy_private_blocks_memory_write(core: NoxCore) -> None:
    await core.security.privacy.set_mode(PrivacyMode.PRIVATE, by="test")
    result = await core.tool_executor.call(
        "nox.chat",
        "memory.write",
        {"text": "should be refused", "source": "test"},
        mode="companion",
    )
    # Either the permission engine itself denies a write tool in PRIVATE mode, or the call is
    # allowed through and `MemoryService.create()` refuses it - either layer blocking is correct;
    # what must never happen is the item actually landing in `memory_items`.
    if result.ok:
        assert result.data is not None
        assert result.data["ok"] is False
    else:
        assert result.error
    rows = core.db.fetch_all("SELECT * FROM memory_items WHERE text = 'should be refused'")
    assert rows == []


async def test_vault_read_tool_reads_written_note(core: NoxCore) -> None:
    write_result = await core.tool_executor.call(
        "nox.chat",
        "vault.append_inbox",
        {"title": "Readback Note", "body": "Readback content.", "source": "test-session"},
        mode="companion",
    )
    assert write_result.data is not None
    path = Path(write_result.data["path"])
    rel_path = path.relative_to(core.config.paths.vault_dir).as_posix()

    read_result = await core.tool_executor.call(
        "nox.chat", "vault.read", {"path": rel_path}, mode="companion"
    )
    assert read_result.ok, read_result.error
    assert read_result.data is not None
    assert "Readback content." in read_result.data["text"]


async def test_embed_provider_goes_through_the_egress_guard(core: NoxCore) -> None:
    """The memory extension's embedding provider must use the guarded client factory.

    It used to build a plain httpx client per request: unguarded, and one fresh SSL context per
    chunk on top of that.
    """
    from nox.security.egress import GuardedTransport, shared_ssl_context

    runtime = core.extensions["memory"]
    provider = runtime.embeddings._provider  # noqa: SLF001 - the point of this test
    assert provider is not None
    client = provider._client_factory()
    try:
        assert isinstance(client._transport, GuardedTransport)
        inner = client._transport._inner
        assert inner._pool._ssl_context is shared_ssl_context()
    finally:
        await client.aclose()


async def test_core_forgets_a_worker_whose_connection_closed(core: NoxCore) -> None:
    """`voice.ptt`/`tts.say` must not be routed to a dead client id after the hub dropped the
    worker (2026-09-15); a re-register brings it back."""
    from nox.core.events import E, Event

    worker = core.workers.attach("voice", "worker:voice")
    worker.registered.set()
    assert core.workers.client_id("voice") == "worker:voice"
    await core.bus.publish(
        Event(
            name=E.IPC_CLIENT_DISCONNECTED,
            payload={"client_id": "worker:voice", "role": "worker", "code": 1011},
        )
    )
    assert core.workers.client_id("voice") is None and worker.client_id is None
