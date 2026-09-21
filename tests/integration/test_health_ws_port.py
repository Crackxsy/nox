"""`/health` must publish the hub port.

The pet window and the dashboard resolve their WebSocket address from this field and fall back to
the default port when it is absent — which left the pet silently offline on any non-default port.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from tests._ports import free_port_base


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[NoxCore]:
    base = free_port_base()
    overrides = {
        "paths": {
            "data_dir": str(tmp_path),
            "vault_dir": str(tmp_path / "vault"),
            "runtime_dir": str(tmp_path / "runtime"),
            "logs_dir": str(tmp_path / "logs"),
        },
        "ipc": {"port": base, "http_port": base + 1},
        "logging": {"level": "WARNING"},
    }
    (tmp_path / "vault").mkdir()
    cfg = load_config(DEFAULTS_PATH, None, None, overrides)
    nox_core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR, extensions=False)
    await asyncio.wait_for(nox_core.start(), timeout=120)
    try:
        yield nox_core
    finally:
        await asyncio.wait_for(nox_core.stop(), timeout=60)


async def test_health_publishes_the_hub_port(core: NoxCore) -> None:
    # Arrange: the core listens on ports no client could guess.
    assert core.hub is not None
    http_port = core.config.ipc.http_port

    # Act
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"http://127.0.0.1:{http_port}/health")

    # Assert: a page served here finds the hub instead of falling back to the default port.
    assert response.status_code == 200
    body = response.json()
    assert body["ws_port"] == core.hub.port
