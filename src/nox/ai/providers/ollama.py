"""OllamaProvider: local models over the Ollama HTTP API.

Endpoints: ``/api/chat`` (NDJSON streaming), ``/api/tags`` (health), ``/api/embed`` with
``/api/embeddings`` fallback (embeddings). The HTTP client comes from an injected factory so the
security agent's egress guard can replace plain httpx without touching this module. GPU policy:
``options.num_gpu=0`` (CPU only) when ``metadata["gpu_allowed"] == "false"`` or when the request
mode is not in ``gpu_allowed_modes`` (never during Rocket League).

Warm models: an Ollama server that has unloaded the model answers the first request about 3.7 s
later than a warm one (measured on ``llama3.2:3b``: 3793 ms to the first token cold, 59 ms warm).
``health()`` therefore doubles as the keep-warm tick - when ``preload`` is on it fires a
zero-message ``/api/chat`` request, which loads the model if needed and pushes its keep-alive out.
That request costs about 130 ms against a loaded model and never blocks the health answer.
"""

from __future__ import annotations

import asyncio
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

#: Budget for the keep-warm request. Long enough to load a small model from a warm page cache,
#: short enough that a wedged server does not leave a task hanging around.
PRELOAD_TIMEOUT_S = 120.0

log = get_logger(__name__)


def default_client_factory() -> httpx.AsyncClient:
    """Plain httpx client (doctor, wizard, tests); production wiring passes the egress guard's
    factory. Shares the process-wide SSL context: a fresh context per request costs ~250 ms of
    synchronous CA-bundle loading and starved the event loop (2026-09-15)."""
    from nox.security.egress import shared_ssl_context  # noqa: PLC0415 - avoids an import cycle

    return httpx.AsyncClient(verify=shared_ssl_context(), timeout=httpx.Timeout(10.0, connect=5.0))


def _refuse_thinking_only(message: dict[str, object]) -> None:
    """Raise instead of returning an empty answer.

    Reasoning models (qwen3, deepseek-r1 and the like) put their output in `message.thinking` and
    leave `message.content` empty, so the caller used to receive a blank answer after a long wait
    with nothing anywhere saying why. An unusable answer has to fail loudly so the router can try
    the next provider.
    """
    thinking = message.get("thinking")
    if isinstance(thinking, str) and thinking.strip():
        raise ProviderError(
            PROVIDER_ID,
            "the model answered only in its thinking channel and left the answer empty; "
            "reasoning models are not supported - choose a non-reasoning model",
            retryable=False,
        )
    raise ProviderError(PROVIDER_ID, "the model returned an empty answer", retryable=True)


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
            # The model name carries the meaning here, and it is the same in both languages.
            display_name_de=f"Ollama ({config.model})",
            local=True,
            roles=roles or [AiRole.CHAT, AiRole.CLASSIFY, AiRole.REASON, AiRole.BACKGROUND],
            status=HealthStatus.UNAVAILABLE,
            reason="not probed yet",
        )
        self._last: dict[str, AiResponse] = {}
        self._warm_task: asyncio.Task[bool] | None = None

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
            self._schedule_warmup()
        else:
            status, reason = HealthStatus.LIMITED, f"model {wanted} not pulled"
        self._info = self._info.model_copy(update={"status": status, "reason": reason})
        return self._info

    # -- keeping the model warm -------------------------------------------------------------------

    def _schedule_warmup(self) -> None:
        """Start a keep-warm request unless one is still running (module docstring)."""
        if not self._cfg.preload:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        self._warm_task = asyncio.get_running_loop().create_task(
            self._warm(), name="nox-ollama-warmup"
        )
        self._warm_task.add_done_callback(self._note_warmup)

    async def warmup(self) -> bool:
        """Load the chat model and refresh its keep-alive. Returns False with a logged reason."""
        return await self._warm()

    async def _warm(self) -> bool:
        payload = {"model": self._cfg.model, "messages": [], "keep_alive": self._cfg.keep_alive}
        try:
            async with self._client_factory() as client:
                response = await client.post(
                    self._url("/api/chat"),
                    json=payload,
                    timeout=httpx.Timeout(PRELOAD_TIMEOUT_S, connect=5.0),
                )
                self._raise_for_status(response)
        except (httpx.HTTPError, ProviderError) as exc:
            log.info(
                "ai.ollama.warmup_failed",
                model=self._cfg.model,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False
        return True

    @staticmethod
    def _note_warmup(task: asyncio.Task[bool]) -> None:
        """Consume the task result so asyncio does not report an unretrieved exception."""
        if not task.cancelled():
            task.exception()

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
        message = data.get("message", {})
        text = str(message.get("content", ""))
        if not text.strip():
            _refuse_thinking_only(message)
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
        saw_thinking = False
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
                    message = chunk.get("message", {})
                    delta = str(message.get("content", ""))
                    if delta:
                        parts.append(delta)
                        yield AiChunk(request_id=request.request_id, delta=delta, done=False)
                    if isinstance(message.get("thinking"), str) and message["thinking"]:
                        saw_thinking = True
                    if chunk.get("done"):
                        tokens_in, tokens_out = self._usage(chunk)
                        break
        except ProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(PROVIDER_ID, f"timeout after {request.timeout_s}s") from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailableError(PROVIDER_ID, f"cannot connect: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(PROVIDER_ID, f"{type(exc).__name__}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(PROVIDER_ID, f"invalid NDJSON: {exc}") from exc
        if not "".join(parts).strip():
            _refuse_thinking_only({"thinking": "yes" if saw_thinking else ""})
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
