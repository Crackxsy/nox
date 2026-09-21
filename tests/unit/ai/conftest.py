"""Shared fakes for the AI tests: an in-memory EventBus and configurable fake providers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from nox.ai.base import AiChunk, AiRequest, AiResponse, AiRole, Message, ProviderInfo
from nox.core.events import HealthStatus
from tests.unit.fakes import FakeBus

__all__ = [
    "FakeBus",
    "FakeProvider",
    "bus",
    "make_request",
]


class FakeProvider:
    """Configurable provider: text, failure, delay, health status, streaming chunks."""

    def __init__(
        self,
        pid: str,
        *,
        local: bool = True,
        roles: list[AiRole] | None = None,
        status: HealthStatus = HealthStatus.AVAILABLE,
        text: str = "ok",
        error: Exception | None = None,
        delay_s: float = 0.0,
        chunks: list[str] | None = None,
        fail_after_chunks: int | None = None,
        tokens: tuple[int, int] | None = (10, 5),
        degraded: bool = False,
        degraded_reason: str = "",
        health_raises: bool = False,
    ) -> None:
        self.pid = pid
        self._local = local
        self._roles = roles or list(AiRole)
        self.status = status
        self.text = text
        self.error = error
        self.delay_s = delay_s
        self.chunks = chunks
        self.fail_after_chunks = fail_after_chunks
        self.tokens = tokens
        self.degraded = degraded
        self.degraded_reason = degraded_reason
        self.health_raises = health_raises
        self.health_calls = 0
        self.complete_calls = 0
        self.stream_calls = 0
        self._last: dict[str, AiResponse] = {}

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            id=self.pid,
            display_name=self.pid,
            local=self._local,
            roles=self._roles,
            status=self.status,
            reason="fake",
        )

    async def health(self) -> ProviderInfo:
        self.health_calls += 1
        if self.health_raises:
            raise RuntimeError("health exploded")
        return self.info

    def _response(self, request: AiRequest) -> AiResponse:
        tokens_in, tokens_out = self.tokens if self.tokens else (None, None)
        return AiResponse(
            request_id=request.request_id,
            provider=self.pid,
            text=self.text,
            latency_ms=1,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            degraded=self.degraded,
            degraded_reason=self.degraded_reason,
        )

    async def complete(self, request: AiRequest) -> AiResponse:
        self.complete_calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None:
            raise self.error
        return self._response(request)

    async def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:
        self.stream_calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.error is not None and self.fail_after_chunks is None:
            raise self.error
        pieces = self.chunks if self.chunks is not None else [self.text]
        for index, piece in enumerate(pieces):
            if self.fail_after_chunks is not None and index >= self.fail_after_chunks:
                assert self.error is not None
                raise self.error
            yield AiChunk(request_id=request.request_id, delta=piece, done=False)
        self._last[request.request_id] = self._response(request).model_copy(
            update={"text": "".join(pieces)}
        )
        yield AiChunk(request_id=request.request_id, delta="", done=True)

    def last_response(self, request_id: str) -> AiResponse | None:
        return self._last.pop(request_id, None)


def make_request(
    text: str = "Hallo Nox",
    *,
    role: AiRole = AiRole.CHAT,
    privacy_mode: str = "balanced",
    request_id: str = "req-1",
    timeout_s: float = 5.0,
    metadata: dict[str, str] | None = None,
    mode: str = "companion",
    system: str | None = None,
) -> AiRequest:
    messages = [Message(role="user", content=text)]
    if system:
        messages.insert(0, Message(role="system", content=system))
    return AiRequest(
        request_id=request_id,
        role=role,
        messages=messages,
        privacy_mode=privacy_mode,
        timeout_s=timeout_s,
        metadata=metadata or {},
        mode=mode,
    )


@pytest.fixture
def bus() -> FakeBus:
    return FakeBus()
