"""Typed view of the ``ai`` section of config/defaults.yaml (router/providers).

The core config agent owns ``nox.core.config``; these models are the AI package's own contract for
the ``ai`` subtree so the router and providers can be constructed from a plain mapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from nox.ai.base import AiRole


class RouterConfig(BaseModel):
    """``ai.router``. Per-role chains default to the fallback chain; ``roles`` overrides them."""

    model_config = ConfigDict(extra="ignore")

    default_reasoner: str = "claude_code"
    fallback_chain: list[str] = Field(default_factory=lambda: ["claude_code", "ollama", "rules"])
    background_budget_share: float = Field(default=0.30, ge=0.0, le=1.0)
    reserve_for_stream: bool = True
    roles: dict[str, list[str]] = Field(default_factory=dict)
    health_ttl_s: float = Field(default=60.0, gt=0.0)
    budget_warmup_tokens: int = Field(default=2000, ge=0)

    def chain_for(self, role: AiRole) -> list[str]:
        """Provider ids to try, in order, for ``role``."""
        explicit = self.roles.get(role.value)
        if explicit:
            return list(explicit)
        if role is AiRole.CLASSIFY:
            return ["rules"]
        if role is AiRole.CODE:
            return ["claude_code"]
        if role is AiRole.REASON:
            rest = [p for p in self.fallback_chain if p != self.default_reasoner]
            return [self.default_reasoner, *rest]
        return list(self.fallback_chain)


class ClaudeCodeConfig(BaseModel):
    """``ai.providers.claude_code``. The CLI brings its own login; no secrets configured here."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    command: str = "claude"
    timeout_s: float = Field(default=120.0, gt=0.0)
    model: str = ""  # empty = CLI default; alias (sonnet, haiku) or full id
    safe_mode: bool = True  # --safe-mode: no user hooks/plugins/MCP in Nox requests
    max_budget_usd: float = Field(default=0.0, ge=0.0)  # 0 = no per-request cap
    health_roundtrip: bool = True  # health() also does a 1-token request (costs quota)


class OllamaConfig(BaseModel):
    """``ai.providers.ollama``."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    base_url: str = "http://127.0.0.1:11434"
    model: str = "llama3.2:3b"
    cpu_model: str = ""  # model when the GPU is not allowed; empty = same model
    embed_model: str = "nomic-embed-text"
    gpu_allowed_modes: list[str] = Field(
        default_factory=lambda: ["companion", "coding", "research", "idle"]
    )
    timeout_s: float = Field(default=60.0, gt=0.0)
    keep_alive: str = "5m"


class ProvidersConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)


class AiConfig(BaseModel):
    """The whole ``ai`` section."""

    model_config = ConfigDict(extra="ignore")

    router: RouterConfig = Field(default_factory=RouterConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> AiConfig:
        """Build from the ``ai`` subtree of a loaded config mapping."""
        return cls.model_validate(dict(data or {}))


def load_ai_config(path: Path) -> AiConfig:
    """Load the ``ai`` section from a YAML config file (defaults.yaml layout)."""
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    section = raw.get("ai", {}) if isinstance(raw, dict) else {}
    return AiConfig.from_mapping(section)
