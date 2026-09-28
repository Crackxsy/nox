"""`nox doctor` does not probe a cloud provider while the configured privacy mode forbids cloud."""

from __future__ import annotations

import pytest

from nox import entrypoints
from nox.ai.base import ProviderInfo
from nox.ai.providers.claude_code import ClaudeCodeProvider
from nox.ai.providers.ollama import OllamaProvider
from nox.core.config import NoxConfig


async def _never(self: ClaudeCodeProvider) -> ProviderInfo:
    raise AssertionError("a cloud provider was probed in a privacy mode that forbids cloud")


async def _local(self: OllamaProvider) -> ProviderInfo:
    return self.info


@pytest.mark.parametrize("mode", ["private", "offline"])
async def test_doctor_skips_cloud_providers_when_privacy_blocks_them(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ClaudeCodeProvider, "health", _never)
    monkeypatch.setattr(OllamaProvider, "health", _local)
    lines: list[str] = []

    await entrypoints._report_providers(
        NoxConfig.model_validate({"privacy": {"mode": mode}}), lines.append
    )

    claude = [line for line in lines if "ai.claude_code" in line]
    assert claude == [
        f"[warn] ai.claude_code: not probed (privacy mode {mode} blocks cloud models)"
    ]
