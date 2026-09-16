"""AiConfig: loads the repo defaults.yaml ai section and derives per-role chains."""

from __future__ import annotations

from pathlib import Path

from nox.ai.base import AiRole
from nox.ai.config import AiConfig, RouterConfig, load_ai_config

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
