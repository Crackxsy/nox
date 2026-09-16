"""The default egress transport reuses one SSL context per process (2026-09-15 incident: a fresh
`httpx.AsyncHTTPTransport()` per request loaded the CA bundle every time, ~250 ms of synchronous
I/O, and starved the event loop during the vault scan)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nox.security import egress as egress_module
from nox.security.egress import EgressGuard, default_transport, shared_ssl_context


def test_shared_ssl_context_is_created_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(egress_module, "_SSL_CONTEXT", None)
    calls = 0
    real = httpx.create_ssl_context

    def counting(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "create_ssl_context", counting)
    first = shared_ssl_context()
    second = shared_ssl_context()
    assert first is second
    assert calls == 1


def test_default_transport_passes_the_shared_context(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real_init = httpx.AsyncHTTPTransport.__init__

    def spy_init(self: Any, *args: Any, **kwargs: Any) -> None:
        seen.append(kwargs.get("verify"))
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "__init__", spy_init)
    default_transport()
    default_transport()
    assert seen == [shared_ssl_context(), shared_ssl_context()]
    assert seen[0] is seen[1]


def test_guard_default_factory_is_the_shared_transport() -> None:
    guard = EgressGuard(profile=lambda: None, privacy=lambda: None)  # type: ignore[arg-type]
    assert guard._transport_factory is default_transport
