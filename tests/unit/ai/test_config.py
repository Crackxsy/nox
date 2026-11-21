"""AiConfig: loads the repo defaults.yaml ai section and derives per-role chains."""

from __future__ import annotations

from pathlib import Path

from nox.ai.base import AiRole
from nox.ai.config import AiConfig, ClaudeCodeConfig, OllamaConfig, RouterConfig, load_ai_config

REPO = Path(__file__).resolve().parents[3]


def test_load_defaults_yaml() -> None:
    cfg = load_ai_config(REPO / "config" / "defaults.yaml")
    assert cfg.router.fallback_chain[-1] == "rules"
    assert cfg.providers.ollama.base_url == "http://127.0.0.1:11434"
    assert "rocket_league" not in cfg.providers.ollama.gpu_allowed_modes
    assert cfg.providers.claude_code.command == "claude"
    assert 0.0 < cfg.router.background_budget_share <= 1.0


def test_from_mapping_ignores_unknown_keys_and_uses_defaults() -> None:
    cfg = AiConfig.from_mapping({"router": {"fallback_chain": ["ollama"], "future_key": 1}})
    assert cfg.router.fallback_chain == ["ollama"]
    assert cfg.providers.ollama.model
    assert AiConfig.from_mapping(None).router.fallback_chain[0] == "claude_code"


def test_chain_for_roles() -> None:
    cfg = RouterConfig(
        default_reasoner="claude_code", fallback_chain=["ollama", "claude_code", "rules"]
    )
    assert cfg.chain_for(AiRole.CHAT) == ["ollama", "claude_code", "rules"]
    assert cfg.chain_for(AiRole.REASON) == ["claude_code", "ollama", "rules"]
    assert cfg.chain_for(AiRole.CODE) == ["claude_code"]
    assert cfg.chain_for(AiRole.CLASSIFY) == ["rules"]
    assert cfg.chain_for(AiRole.BACKGROUND) == ["ollama", "claude_code", "rules"]
    cfg2 = RouterConfig(roles={"classify": ["ollama", "rules"]})
    assert cfg2.chain_for(AiRole.CLASSIFY) == ["ollama", "rules"]


def test_the_default_local_model_is_one_name_in_the_code_and_the_defaults_file() -> None:
    """Three places spell the model Nox asks Ollama for; a drift between them once shipped a
    default that the docs and the shipped file said was something else."""
    from nox.ai.config import DEFAULT_OLLAMA_MODEL
    from nox.core.config import OllamaProviderConfig

    shipped = load_ai_config(REPO / "config" / "defaults.yaml").providers.ollama.model
    assert shipped == DEFAULT_OLLAMA_MODEL
    assert OllamaConfig().model == DEFAULT_OLLAMA_MODEL
    assert OllamaProviderConfig().model == DEFAULT_OLLAMA_MODEL


def test_the_health_probe_that_costs_quota_is_off_everywhere_by_default() -> None:
    from nox.core.config import ClaudeCodeProviderConfig

    shipped = load_ai_config(REPO / "config" / "defaults.yaml").providers.claude_code
    assert shipped.health_roundtrip is False
    assert ClaudeCodeConfig().health_roundtrip is False
    assert ClaudeCodeProviderConfig().health_roundtrip is False
