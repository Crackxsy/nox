"""Stream Bot core integration: boots the real `NoxCore` headless, registers a fake
`twitch.chat.send` tool (standing in for the not-yet-relevant twitch plugin) directly on
`core.tool_registry`, and drives an addressed chat message through the real bus. Asserts the
message reaches `StreamResponder`, which answers through the real `ToolExecutor` pipeline (the fake
tool is actually invoked, and the call is audited) - end to end, no mocking of the wiring in
`nox.app`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from nox.app import DEFAULTS_PATH, PROFILES_DIR, NoxCore
from nox.core.config import load_config
from nox.core.events import E, Event
from nox.security.model import Risk
from nox.tools.registry import ToolSpec
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
        # `twitch*` tools are classified "cloud" (Security Model CLOUD_TOOL_PATTERNS) and are
        # therefore denied outright in PRIVATE/OFFLINE privacy mode - stays at the BALANCED
        # default so the "stream" profile's `stream.twitch.chat: allow` rule can apply.
        "privacy": {"mode": "balanced"},
        "health": {"check_interval_s": 3600},
        "logging": {"level": "WARNING"},
        "stream": {"relevance": {"channel_cooldown_s": 0.0}},
    }
    (tmp_path / "vault").mkdir()
    return load_config(DEFAULTS_PATH, None, None, overrides)


@pytest.fixture
async def core(tmp_path: Path):
    cfg = _config(tmp_path)
    core = NoxCore(cfg, voice=False, profiles_dir=PROFILES_DIR)
    await core.start()
    # Stream Bot tools (`twitch*`) are only in the "stream" profile's `integrations_allowed`
    # (config/profiles/stream.yaml); the default "companion" profile denies them (FR-9.x). Mirror
    # what `mode.set` does for a real "stream" mode switch instead of going through IPC.
    core.security.engine.set_profile("stream", by="test")
    try:
        yield core
    finally:
        await core.stop()


class _ChatSendInput(BaseModel):
    text: str


def _register_fake_twitch_chat_send(core: NoxCore) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"sent": True, "text": payload["text"]}

    core.tool_registry.register(
        ToolSpec(
            name="twitch.chat.send",
            description="test double for the twitch plugin's chat.send tool",
            input_model=_ChatSendInput,
            risk=Risk.LOW,
            handler=handler,
        )
    )
    return calls


async def test_addressed_chat_message_reaches_twitch_chat_send_through_the_executor(
    core: NoxCore,
) -> None:
    calls = _register_fake_twitch_chat_send(core)
    await core.bus.publish(
        Event(
            name=E.TWITCH_CHAT_MESSAGE,
            payload={
                "chat_event_id": 1,
                "viewer_id": "v1",
                "text": "hey nox, was geht?",
                "channel": "public",
                "relevance": 1.0,
                "addressed_to_nox": True,
            },
        )
    )
    assert len(calls) == 1
    assert calls[0]["text"]

    # The permission check and the tool execution each audit their own entry (`nox.security.
    # permissions.DefaultPermissionEngine._record` and `nox.tools.executor.ToolExecutor._finish`)
    # - assert on presence/outcome, not on the exact count of internal audit points.
    audited = [
        e
        for e in core.security.audit.entries(limit=200)
        if e.tool == "twitch" and e.action == "chat.send"
    ]
    assert audited
    assert all(e.decision == "allow" and e.result == "ok" for e in audited)


async def test_non_addressed_low_relevance_message_does_not_call_the_tool(core: NoxCore) -> None:
    calls = _register_fake_twitch_chat_send(core)
    await core.bus.publish(
        Event(
            name=E.TWITCH_CHAT_MESSAGE,
            payload={
                "chat_event_id": 2,
                "viewer_id": "v2",
                "text": "just chatting",
                "channel": "public",
                "relevance": 0.0,
                "addressed_to_nox": False,
            },
        )
    )
    assert calls == []
