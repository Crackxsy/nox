"""Wiring for the dashboard's local-model setup: the `ai.setup.*` requests on the running core.

Everything the flow needs comes from the core as it is at the moment a run starts: the live
privacy mode (so switching to `offline` in the tray stops the next download), the egress guard's
client, the Ollama settings, and whether a `package.install` tool has been registered by then.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

import httpx

from nox.ai.config import AiConfig
from nox.core.logging import get_logger
from nox.core.state import PrivacyMode
from nox.localai.hardware import detect_hardware
from nox.localai.ipc import LocalAiService, register_localai_ipc
from nox.localai.ollama_setup import OllamaInstaller, tool_installer
from nox.localai.setup import SetupDeps
from nox.settings.layers import resolve_user_config_path

log = get_logger(__name__)

__all__ = ["install"]

_DEFAULT_MODE = "companion"


class _Core(Protocol):
    """The subset of the core this module needs (structural, so a test needs no real core)."""

    config: Any
    security: Any
    state: Any
    registry: Any
    tool_registry: Any
    tool_executor: Any


def _mode(core: _Core) -> str:
    try:
        value = core.state.get("assistant.mode")
    except Exception:  # noqa: BLE001 - a missing mode must not break an install
        return _DEFAULT_MODE
    return str(value) if value else _DEFAULT_MODE


def install(core: _Core) -> LocalAiService:
    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return cast(httpx.AsyncClient, core.security.egress.client(**kwargs))

    def installer() -> OllamaInstaller | None:
        return tool_installer(core.tool_registry, core.tool_executor, mode=lambda: _mode(core))

    def deps() -> SetupDeps:
        ai = AiConfig.from_mapping(core.config.ai.model_dump())
        return SetupDeps(
            ollama=ai.providers.ollama,
            client_factory=client_factory,
            privacy_mode=lambda: PrivacyMode(core.security.privacy.mode),
            user_config_path=resolve_user_config_path(),
            detect=detect_hardware,
            installer=installer,
        )

    service = LocalAiService(deps)
    register_localai_ipc(core.registry, service)
    log.info("localai.installed")
    return service
