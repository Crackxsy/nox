"""`NoxConfig`: the whole validated configuration of one Nox installation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field, PrivateAttr, model_validator

from nox.core.config.assistant import (
    AiConfig,
    AttentionConfig,
    MemoryConfig,
    PetConfig,
    ProactiveConfig,
    SensorsConfig,
    VoiceConfig,
)
from nox.core.config.core import (
    DEFAULT_DATA_DIR,
    HealthConfig,
    IdentityConfig,
    IpcConfig,
    LoggingConfig,
    PathsConfig,
    PluginsConfig,
    SupervisorConfig,
)
from nox.core.config.features import (
    CLIP_ROOTS,
    ClipsConfig,
    CreativeConfig,
    PmConfig,
    RemoteConfig,
    RlConfig,
    StreamConfig,
)
from nox.core.config.security import PrivacyConfig, SecurityConfig
from nox.core.config.types import ConfigWarning, StrictSection, expand_path

__all__ = ["NoxConfig"]


class NoxConfig(StrictSection):
    """The merged, validated configuration. Mirrors `config/defaults.yaml` section for section."""

    schema_version: int = Field(default=1, ge=1, le=1)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    #: Every sub-directory is filled in by `PathsConfig` itself when it is not set, so the
    #: factory never has to name one.
    paths: PathsConfig = Field(default_factory=lambda: PathsConfig.model_validate({}))
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    ipc: IpcConfig = Field(default_factory=IpcConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    pet: PetConfig = Field(default_factory=PetConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
    attention: AttentionConfig = Field(default_factory=AttentionConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    rl: RlConfig = Field(default_factory=RlConfig)
    clips: ClipsConfig = Field(default_factory=ClipsConfig)
    pm: PmConfig = Field(default_factory=PmConfig)
    creative: CreativeConfig = Field(default_factory=CreativeConfig)
    sensors: SensorsConfig = Field(default_factory=SensorsConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    _warnings: list[ConfigWarning] = PrivateAttr(default_factory=list)
    _profile_id: str | None = PrivateAttr(default=None)

    @classmethod
    def loaded(
        cls,
        config: NoxConfig,
        *,
        warnings: list[ConfigWarning],
        profile_id: str | None,
    ) -> NoxConfig:
        """Attach what the loader learned while assembling `config`, and return it.

        Which layers were rejected and which profile applied are not configuration and must not be
        settable from a layer, so they are private attributes - written here, inside the model,
        rather than by a module-level function reaching into it.
        """
        config._warnings = list(warnings)
        config._profile_id = profile_id
        return config

    @model_validator(mode="before")
    @classmethod
    def _derive_clip_roots(cls, data: Any) -> Any:
        """Put the clip roots under `paths.data_dir` unless the user named them explicitly.

        This lives on the root model because `ClipsConfig` cannot see `paths`; its own defaults
        assume the default data directory, which is right only when nobody moved it.
        """
        if not isinstance(data, Mapping):
            return data
        values = dict(data)
        paths = values.get("paths")
        raw_data_dir = paths.get("data_dir") if isinstance(paths, Mapping) else None
        base = expand_path(str(raw_data_dir or DEFAULT_DATA_DIR)) / "data" / "clips"
        clips = dict(values.get("clips") or {})
        for field, subdirectory in CLIP_ROOTS.items():
            if not clips.get(field):
                clips[field] = base / subdirectory
        values["clips"] = clips
        return values

    @property
    def warnings(self) -> list[ConfigWarning]:
        """Problems found while loading (rejected layers). Empty when every layer applied."""
        return list(self._warnings)

    @property
    def profile_id(self) -> str | None:
        """The profile layer that was applied, or None."""
        return self._profile_id

    @property
    def log_retention_days(self) -> int:
        """`logging.retention_days` when set, otherwise the general log retention."""
        return self.logging.retention_days or self.privacy.retention.logs_days
