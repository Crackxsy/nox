"""OllamaProvider against httpx.MockTransport: health, chat, streaming, GPU policy, embeddings."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from nox.ai.base import AiRole
from nox.ai.config import OllamaConfig
from nox.ai.errors import ProviderError, ProviderTimeoutError, ProviderUnavailableError
from nox.ai.providers.ollama import OllamaProvider
from nox.core.events import HealthStatus

from .conftest import make_request

TAGS = {"models": [{"name": "llama3.2:3b"}, {"name": "nomic-embed-text:latest"}]}


class Recorder:
    """Collects requests and answers per path."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"error": "not found"})
        return route(request)

    def factory(self) -> Callable[[], httpx.AsyncClient]:
        return lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def body(self, index: int = -1) -> dict[str, object]:
        result: dict[str, object] = json.loads(self.requests[index].content)
        return result


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


def provider(rec: Recorder, **cfg: object) -> OllamaProvider:
    return OllamaProvider(OllamaConfig(**cfg), rec.factory())  # type: ignore[arg-type]


async def test_health_available_limited_unavailable(rec: Recorder) -> None:
    rec.routes["/api/tags"] = lambda _r: httpx.Response(200, json=TAGS)
    p = provider(rec)
    assert (await p.health()).status is HealthStatus.AVAILABLE
    p2 = provider(rec, model="mistral")
    info = await p2.health()
    assert info.status is HealthStatus.LIMITED and "not pulled" in info.reason

    def down(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    rec.routes["/api/tags"] = down
    assert (await p.health()).status is HealthStatus.UNAVAILABLE
    assert p.info.status is HealthStatus.UNAVAILABLE


async def test_complete_parses_text_and_usage(rec: Recorder) -> None:
    rec.routes["/api/chat"] = lambda _r: httpx.Response(
        200,
        json={
            "message": {"role": "assistant", "content": "Hallo!"},
            "done": True,
            "prompt_eval_count": 12,
            "eval_count": 3,
        },
    )
    p = provider(rec)
    response = await p.complete(make_request("Hallo", system="Du bist Nox."))
    assert response.text == "Hallo!"
    assert (response.tokens_in, response.tokens_out) == (12, 3)
    assert response.provider == "ollama" and response.degraded is False
    body = rec.body()
    assert body["model"] == "llama3.2:3b" and body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "Du bist Nox."},
        {"role": "user", "content": "Hallo"},
    ]
    assert body["keep_alive"] == "5m"
    assert "num_gpu" not in body["options"]  # type: ignore[operator]


async def test_stream_parses_ndjson_and_records_usage(rec: Recorder) -> None:
    lines = [
        {"message": {"content": "Hal"}, "done": False},
        {"message": {"content": "lo"}, "done": False},
        {"message": {"content": ""}, "done": True, "prompt_eval_count": 7, "eval_count": 2},
    ]
    rec.routes["/api/chat"] = lambda _r: httpx.Response(
        200, content="\n".join(json.dumps(x) for x in lines) + "\n"
    )
    p = provider(rec)
    chunks = [c async for c in p.stream(make_request())]
    assert [c.delta for c in chunks] == ["Hal", "lo", ""]
    assert chunks[-1].done is True
    last = p.last_response("req-1")
    assert last is not None and last.text == "Hallo" and last.tokens_out == 2
    assert p.last_response("req-1") is None
    assert rec.body()["stream"] is True


async def test_stream_error_line_raises(rec: Recorder) -> None:
    rec.routes["/api/chat"] = lambda _r: httpx.Response(
        200, content=json.dumps({"error": "model crashed"}) + "\n"
    )
    with pytest.raises(ProviderError, match="model crashed"):
        async for _ in provider(rec).stream(make_request()):
            pass


@pytest.mark.parametrize(
    ("mode", "metadata", "expect_cpu"),
    [
        ("companion", {}, False),
        ("rocket_league", {}, True),
        ("companion", {"gpu_allowed": "false"}, True),
        ("rocket_league", {"gpu_allowed": "true"}, True),
    ],
)
async def test_gpu_policy_sets_num_gpu_zero(
    rec: Recorder, mode: str, metadata: dict[str, str], expect_cpu: bool
) -> None:
    rec.routes["/api/chat"] = lambda _r: httpx.Response(200, json={"message": {"content": "x"}})
    p = provider(rec, cpu_model="llama3.2:1b")
    request = make_request(mode=mode, metadata=metadata)
    await p.complete(request)
    options = rec.body()["options"]
    assert isinstance(options, dict)
    assert (options.get("num_gpu") == 0) is expect_cpu
    assert rec.body()["model"] == ("llama3.2:1b" if expect_cpu else "llama3.2:3b")


async def test_metadata_model_override(rec: Recorder) -> None:
    rec.routes["/api/chat"] = lambda _r: httpx.Response(200, json={"message": {"content": "x"}})
    await provider(rec).complete(make_request(metadata={"model": "mistral"}))
    assert rec.body()["model"] == "mistral"


async def test_404_is_non_retryable_provider_error(rec: Recorder) -> None:
    rec.routes["/api/chat"] = lambda _r: httpx.Response(404, json={"error": "model not found"})
    with pytest.raises(ProviderError) as exc_info:
        await provider(rec).complete(make_request())
    assert exc_info.value.retryable is False


async def test_connect_error_and_timeout_map_to_typed_errors(rec: Recorder) -> None:
    def refused(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    rec.routes["/api/chat"] = refused
    with pytest.raises(ProviderUnavailableError):
        await provider(rec).complete(make_request())
    rec.routes["/api/chat"] = slow
    with pytest.raises(ProviderTimeoutError):
        await provider(rec).complete(make_request())
    with pytest.raises(ProviderTimeoutError):
        async for _ in provider(rec).stream(make_request()):
            pass


async def test_embed_batch_and_legacy_fallback(rec: Recorder) -> None:
    rec.routes["/api/embed"] = lambda _r: httpx.Response(
        200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
    )
    p = provider(rec)
    vectors = await p.embed(["a", "b"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]
    assert rec.body()["model"] == "nomic-embed-text"
    assert await p.embed([]) == []

    rec.routes["/api/embed"] = lambda _r: httpx.Response(404)
    rec.routes["/api/embeddings"] = lambda r: httpx.Response(
        200, json={"embedding": [float(len(json.loads(r.content)["prompt"]))]}
    )
    assert await p.embed(["ab", "abc"]) == [[2.0], [3.0]]


async def test_embed_shape_mismatch_raises(rec: Recorder) -> None:
    rec.routes["/api/embed"] = lambda _r: httpx.Response(200, json={"embeddings": [[0.1]]})
    with pytest.raises(ProviderError):
        await provider(rec).embed(["a", "b"])


def test_info_is_local_and_supports_chat() -> None:
    p = OllamaProvider(OllamaConfig())
    assert p.info.local is True
    assert AiRole.CHAT in p.info.roles and AiRole.CODE not in p.info.roles


@pytest.mark.network
@pytest.mark.spike
async def test_real_ollama_roundtrip() -> None:
    p = OllamaProvider(OllamaConfig())
    info = await p.health()
    if info.status is HealthStatus.UNAVAILABLE:
        pytest.skip(f"ollama not reachable: {info.reason}")
    response = await p.complete(make_request("Antworte mit genau einem Wort: Hallo", timeout_s=120))
    assert response.text.strip()
