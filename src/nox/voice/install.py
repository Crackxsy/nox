"""Wiring for the dashboard's voice-model download: the `voice.model.*` requests on the core.

Everything is read when it is needed, not at install time: the configured engine and models root
from the configuration, and the privacy mode live from the security core, so switching to
`offline` in the tray stops the next download before it starts - and, through the
`privacy.mode_changed` event, a download that is already running. The downloader process joins the
workers' job object, so it cannot outlive the core.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nox.voice._logging import get_logger
from nox.voice.model_setup import configured_models_root
from nox.voice.service import VoiceModelService, register_voice_model_ipc

log = get_logger(__name__)

__all__ = ["install"]


def install(core: Any) -> VoiceModelService:
    config = core.config

    def engine() -> str:
        return str(config.voice.tts.engine)

    def root() -> Path:
        return configured_models_root(config.voice.models_dir, config.paths.data_dir)

    def privacy_mode() -> str:
        security = getattr(core, "security", None)
        if security is None:
            return str(config.privacy.mode)
        return str(security.privacy.mode.value)

    def joined(pid: int) -> None:
        workers = getattr(core, "workers", None)
        job = getattr(workers, "job", None)
        if job is not None:
            job.assign(pid)

    service = VoiceModelService(
        engine=engine, models_root=root, privacy_mode=privacy_mode, on_spawn=joined
    )
    register_voice_model_ipc(core.registry, service)
    bus = getattr(core, "bus", None)
    if bus is not None:
        service.watch(bus)
    log.info("voice.model_service_installed")
    return service
