"""Walking-skeleton integration test: boots the real core headless (no voice worker, no shell) with
temporary paths, talks to it over the real IPC hub as a dashboard client, drives a chat turn
through the router (OFFLINE privacy mode: cloud providers filtered out, local Ollama/rules stay),
engages the kill switch and shuts down cleanly."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.ipc.client import IpcClient
from nox.ipc.errors import IpcError
from nox.ipc.tokens import read_session_token


def _config(tmp_path: Path):
    base = random.randint(20000, 60000)  # noqa: S311
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
        "privacy": {"mode": "offline"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
    }
    (tmp_path / "vault").mkdir()
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
async def core(tmp_path: Path):
    cfg = _config(tmp_path)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
    await asyncio.wait_for(core.start(), timeout=60)
    try:
        yield core
    finally:
        await asyncio.wait_for(core.stop(), timeout=30)


async def _client(core: NoxCore, role: str = "dashboard") -> IpcClient:
    token = read_session_token(Path(core.config.paths.runtime_dir))
    client = IpcClient(core.hub.url, token, role, f"test-{role}", client_version="0.1.0")
    await client.connect()
    return client


async def test_boot_state_and_health(core: NoxCore):
    assert core.hub.url.startswith("ws://127.0.0.1:")
    assert (Path(core.config.paths.runtime_dir) / "session.token").exists()
    client = await _client(core)
    try:
        state = await client.request("state.get", {})
        assert state["privacy"]["mode"] == "offline"
        assert state["system"]["level"] == "running"
        health = await client.request("health.get", {})
        assert "ai.rules" in health["components"]
        assert health["components"]["ai.rules"]["status"] == "available"
        providers = await client.request("ai.providers", {})
        ids = {p["id"] for p in providers["providers"]}
        assert {"rules", "ollama", "claude_code"} <= ids
    finally:
        await client.close()


async def test_chat_turn_offline_uses_rules_and_streams(core: NoxCore):
    client = await _client(core)
    chunks: list[str] = []
    try:
        call = await client.request_stream(
            "chat.send", {"text": "Hallo Nox", "speak": False}, timeout=30
        )
        async for frame in call:
            chunks.append(str(frame.get("delta", "")))
        result = await call.result()
        assert result["text"]
        # OFFLINE blocks cloud providers only; Ollama on loopback stays allowed (local=True).
        assert result["provider"] in ("ollama", "rules")
        if result["provider"] == "rules":
            assert result["degraded"] is True
        assert "".join(chunks) == result["text"]
        # the turn was recorded (offline mode still allows local, text-only memory)
        rows = core.db.fetch_all("SELECT role, text FROM turns ORDER BY id")
        assert [r["role"] for r in rows] == ["user", "assistant"]
    finally:
        await client.close()


async def test_pet_role_is_restricted(core: NoxCore):
    pet = await _client(core, role="pet")
    try:
        state = await pet.request("state.get", {})
        assert set(state) == {"assistant", "privacy", "system"}
        with pytest.raises(Exception, match="may not call|permission"):
            await pet.request("security.kill", {"reason": "nope"})
    finally:
        await pet.close()


async def test_unauthenticated_or_wrong_token_is_rejected(core: NoxCore):
    client = IpcClient(core.hub.url, "x" * 40, "dashboard", "bad", client_version="0.1.0")
    with pytest.raises(IpcError):
        await client.connect()


async def test_kill_switch_enters_safe_mode_and_resume(core: NoxCore):
    client = await _client(core)
    try:
        res = await client.request("security.kill", {"reason": "test", "origin": "ui"})
        assert res["ok"] is True
        assert core.security.killswitch.is_engaged()
        state = await client.request("state.get", {})
        assert state["system"]["level"] == "safe_mode"
        assert state["assistant"]["pet_functional"] == "unavailable"
        with pytest.raises(IpcError):
            await client.request("chat.send", {"text": "noch da?", "speak": False}, timeout=10)
        audit = core.db.fetch_all("SELECT action FROM audit_log")
        assert any("kill" in str(r["action"]) for r in audit)
        res = await client.request("security.resume", {})
        assert res["ok"] is True
        state = await client.request("state.get", {})
        assert state["system"]["level"] == "running"
    finally:
        await client.close()


async def test_audit_chain_verifies_after_activity(core: NoxCore):
    verification = core.security.audit.verify_chain_detailed()
    assert verification.ok, str(verification)


async def test_http_serves_built_ui_with_hardened_headers(core: NoxCore):
    import httpx

    async with httpx.AsyncClient(base_url=core.http.url, timeout=10) as http:
        health = await http.get("/health")
        assert health.status_code == 200 and "components" in health.json()
        bundles_built = core.pet_dist.exists() and core.dashboard_dist.exists()
        for path in ("/pet/", "/dashboard/"):
            r = await http.get(path, follow_redirects=True)
            if bundles_built:
                assert r.status_code == 200, path
                assert "assets/" in r.text, f"{path} should serve the built bundle"
            else:  # CI python job runs without `npm run build`: honest 503 placeholder, no fake UI
                assert r.status_code == 503, path
                assert "not built" in r.text.lower(), f"{path} should serve the placeholder"
            assert r.headers.get("referrer-policy") == "no-referrer"
            assert r.headers.get("cache-control") == "no-store"
        # /api needs the session token as bearer; never accepted as a query parameter
        assert (await http.get("/api/state")).status_code == 401
        token = read_session_token(Path(core.config.paths.runtime_dir))
        assert (await http.get(f"/api/state?token={token}")).status_code == 401
        ok = await http.get("/api/state", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200 and "assistant" in ok.json()
