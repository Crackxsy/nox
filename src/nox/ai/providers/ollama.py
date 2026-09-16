"""OllamaProvider: local models over the Ollama HTTP API (ADR-008, FR-6.5, SP-12).

Endpoints: ``/api/chat`` (NDJSON streaming), ``/api/tags`` (health), ``/api/embed`` with
``/api/embeddings`` fallback (embeddings). The HTTP client comes from an injected factory so the
security agent's egress guard can replace plain httpx without touching this module.
GPU policy: ``options.num_gpu=0`` (CPU only) when ``metadata["gpu_allowed"] == "false"`` or when the
request mode is not in ``gpu_allowed_modes`` (never during Rocket League).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from nox.ai._log import get_logger
from nox.ai.base import AiChunk, AiRequest, AiResponse, AiRole, ProviderInfo
from nox.ai.config import OllamaConfig
from nox.ai.errors import ProviderError, ProviderTimeoutError, ProviderUnavailableError
from nox.core.events import HealthStatus

PROVIDER_ID = "ollama"
ClientFactory = Callable[[], httpx.AsyncClient]

log = get_logger(__name__)


def default_client_factory() -> httpx.AsyncClient:
    """Plain httpx client (doctor, wizard, tests); production wiring passes the egress guard's
    factory. Shares the process-wide SSL context: a fresh context per request costs ~250 ms of
    synchronous CA-bundle loading and starved the event loop (2026-09-15)."""
    from nox.security.egress import shared_ssl_context  # noqa: PLC0415 - avoids an import cycle

    return httpx.AsyncClient(verify=shared_ssl_context(), timeout=httpx.Timeout(10.0, connect=5.0))


class OllamaProvider:
    """Local LLM provider. Never leaves the machine (``local=True``)."""

    def __init__(
        self,
        config: OllamaConfig,
        client_factory: ClientFactory | None = None,
        *,
        roles: list[AiRole] | None = None,
    ) -> None:
        self._cfg = config
        self._client_factory = client_factory or default_client_factory
        self._info = ProviderInfo(
            id=PROVIDER_ID,
            display_name=f"Ollama ({config.model})",
            local=True,
            roles=roles or [AiRole.CHAT, AiRole.CLASSIFY, AiRole.REASON, AiRole.BACKGROUND],
            status=HealthStatus.UNAVAILABLE,
            reason="not probed yet",
        )
        self._last: dict[str, AiResponse] = {}

    @property
    def info(self) -> ProviderInfo:
        return self._info

    @property
    def config(self) -> OllamaConfig:
        return self._cfg

    def _url(self, path: str) -> str:
        return self._cfg.base_url.rstrip("/") + path

    # -- health -------------------------------------------------------------------------------

    async def health(self) -> ProviderInfo:
        """GET /api/tags; AVAILABLE if the configured model is pulled, LIMITED if the server runs
        but the model is missing, UNAVAILABLE if the server does not answer."""
        try:
            async with self._client_factory() as client:
                response = await client.get(self._url("/api/tags"), timeout=5.0)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._info = self._info.model_copy(
                update={
                    "status": HealthStatus.UNAVAILABLE,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            return self._info
        names = {m.get("name", "") for m in payload.get("models", [])}
        names |= {n.split(":")[0] for n in names}
        wanted = self._cfg.model
        if wanted in names or wanted.split(":")[0] in names:
            status, reason = HealthStatus.AVAILABLE, f"model {wanted} available"
        else:
            status, reason = HealthStatus.LIMITED, f"model {wanted} not pulled"
        self._info = self._info.model_copy(update={"status": status, "reason": reason})
        return self._info

    # -- request building -----------------------------------------------------------------------

    def gpu_allowed(self, request: AiRequest) -> bool:
        if request.metadata.get("gpu_allowed", "").lower() == "false":
            return False
        return request.mode in self._cfg.gpu_allowed_modes

    def model_for(self, request: AiRequest) -> str:
        override = request.metadata.get("model", "")
        if override:
            return override
        if not self.gpu_allowed(request) and self._cfg.cpu_model:
            return self._cfg.cpu_model
        return self._cfg.model

    def build_payload(self, request: AiRequest, *, stream: bool) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": request.temperature,
            "num_predict": request.max_tokens,
        }
        if not self.gpu_allowed(request):
            options["num_gpu"] = 0
        return {
            "model": self.model_for(request),
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": stream,
            "options": options,
            "keep_alive": self._cfg.keep_alive,
        }

    def _timeout(self, request: AiRequest) -> httpx.Timeout:
        total = min(request.timeout_s, self._cfg.timeout_s)
        return httpx.Timeout(total, connect=5.0)

    @staticmethod
    def _usage(chunk: dict[str, Any]) -> tuple[int | None, int | None]:
        tokens_in = chunk.get("prompt_eval_count")
        tokens_out = chunk.get("eval_count")
        return (
            int(tokens_in) if isinstance(tokens_in, int) else None,
            int(tokens_out) if isinstance(tokens_out, int) else None,
        )

    # -- AiProvider -----------------------------------------------------------------------------

    async def complete(self, request: AiRequest) -> AiResponse:
        started = time.perf_counter()
        payload = self.build_payload(request, stream=False)
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    self._url("/api/chat"), json=payload, timeout=self._timeout(request)
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(PROVIDER_ID, f"timeout after {request.timeout_s}s") from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(PROVIDER_ID, f"cannot connect: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(PROVIDER_ID, f"{type(exc).__name__}: {exc}") from exc
        self._raise_for_status(response)
        data = response.json()
        text = str(data.get("message", {}).get("content", ""))
        tokens_in, tokens_out = self._usage(data)
        result = AiResponse(
            request_id=request.request_id,
            provider=PROVIDER_ID,
            text=text,
            latency_ms=int((time.perf_counter() - started) * 1000),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        self._last[request.request_id] = result
        return result

    async def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:
        started = time.perf_counter()
        payload = self.build_payload(request, stream=True)
        parts: list[str] = []
        tokens_in: int | None = None
        tokens_out: int | None = None
        try:
            async with (
                self._client_factory() as client,
                client.stream(
                    "POST", self._url("/api/chat"), json=payload, timeout=self._timeout(request)
                ) as response,
            ):
                self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise ProviderError(PROVIDER_ID, str(chunk["error"]))
                    delta = str(chunk.get("message", {}).get("content", ""))
                    if delta:
                        parts.append(delta)
                        yield AiChunk(request_id=request.request_id, delta=delta, done=False)
                    if chunk.get("done"):
                        tokens_in, tokens_out = self._usage(chunk)
                        break
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(PROVIDER_ID, f"timeout after {request.timeout_s}s") from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(PROVIDER_ID, f"cannot connect: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(PROVIDER_ID, f"{type(exc).__name__}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(PROVIDER_ID, f"invalid NDJSON: {exc}") from exc
        self._last[request.request_id] = AiResponse(
            request_id=request.request_id,
            provider=PROVIDER_ID,
            text="".join(parts),
            latency_ms=int((time.perf_counter() - started) * 1000),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        yield AiChunk(request_id=request.request_id, delta="", done=True)

    def last_response(self, request_id: str) -> AiResponse | None:
        """Usage of the last streamed request (the router reads it after the stream finished)."""
        return self._last.pop(request_id, None)

    # -- embeddings -----------------------------------------------------------------------------

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        """Embed ``texts`` via ``/api/embed`` (batch); fallback ``/api/embeddings`` per text."""
        if not texts:
            return []
        use_model = model or self._cfg.embed_model
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    self._url("/api/embed"),
                    json={"model": use_model, "input": texts, "keep_alive": self._cfg.keep_alive},
                    timeout=httpx.Timeout(self._cfg.timeout_s, connect=5.0),
                )
                if response.status_code == 404:
                    return await self._embed_legacy(client, use_model, texts)
                self._raise_for_status(response)
                data = response.json()
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(PROVIDER_ID, f"cannot connect: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(PROVIDER_ID, f"{type(exc).__name__}: {exc}") from exc
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ProviderError(PROVIDER_ID, "embed: unexpected response shape", retryable=False)
        return [[float(x) for x in vec] for vec in vectors]

    async def _embed_legacy(
        self, client: httpx.AsyncClient, model: str, texts: list[str]
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            response = await client.post(
                self._url("/api/embeddings"),
                json={"model": model, "prompt": text},
                timeout=httpx.Timeout(self._cfg.timeout_s, connect=5.0),
            )
            self._raise_for_status(response)
            vectors.append([float(x) for x in response.json().get("embedding", [])])
        return vectors

    # -- helpers --------------------------------------------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 404:
            raise ProviderError(
                PROVIDER_ID, "model not found (404): pull it first", retryable=False
            )
        if response.status_code >= 400:
            raise ProviderError(PROVIDER_ID, f"HTTP {response.status_code}")
