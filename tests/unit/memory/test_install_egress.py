"""The embedding provider is only built when the egress guard is there to police it.

Embedding a note sends its text to a local model server. That request is an outbound request like
any other, so without the guard the feature is switched off and says so, rather than sending the
text anyway with a warning in the log.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nox.core.events import HealthStatus
from nox.memory.install import _build_embed_provider


def _core(*, egress: Any, ollama_enabled: bool = True) -> Any:
    providers = SimpleNamespace(ollama=SimpleNamespace(enabled=ollama_enabled, base_url="http://x"))
    return SimpleNamespace(
        config=SimpleNamespace(ai=SimpleNamespace(providers=providers)),
        security=SimpleNamespace(egress=egress),
    )


class FakeEgress:
    def client(self, **_kwargs: Any) -> Any:
        return object()


def test_no_egress_guard_means_no_embedding_provider() -> None:
    provider, reason = _build_embed_provider(_core(egress=None), "nomic-embed-text")

    assert provider is None
    assert "egress guard" in reason


def test_the_guard_is_used_to_build_the_client() -> None:
    provider, reason = _build_embed_provider(_core(egress=FakeEgress()), "nomic-embed-text")

    assert provider is not None
    assert reason == ""


def test_disabled_ollama_is_not_an_error() -> None:
    provider, reason = _build_embed_provider(
        _core(egress=FakeEgress(), ollama_enabled=False), "nomic-embed-text"
    )

    assert provider is None
    assert reason == ""  # switched off on purpose, so nothing is unavailable


async def test_the_health_check_reports_the_reason() -> None:
    from nox.memory.install import MemoryRuntime

    runtime = MemoryRuntime(
        embeddings=None,  # type: ignore[arg-type]
        indexer=None,  # type: ignore[arg-type]
        watcher=None,  # type: ignore[arg-type]
        writer=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        retrieval=None,  # type: ignore[arg-type]
        retention=None,  # type: ignore[arg-type]
        embeddings_unavailable="no egress guard on the security layer, so embeddings stay disabled",
    )

    status, reason = await runtime.health()

    assert status is HealthStatus.UNAVAILABLE
    assert "egress guard" in reason
