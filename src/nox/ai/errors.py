"""Exceptions of the AI layer (ADR-008): providers raise ProviderError, the router RouterError."""

from __future__ import annotations


class ProviderError(Exception):
    """A provider could not fulfil a request (network, process, protocol or model failure)."""

    def __init__(self, provider: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.message = message
        self.retryable = retryable


class ProviderUnavailableError(ProviderError):
    """The provider is not usable at all right now (not installed, not logged in, service down)."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(provider, message, retryable=False)


class ProviderTimeoutError(ProviderError):
    """The provider did not answer within the request timeout."""


class RouterError(Exception):
    """The router could not produce a response."""


class NoProviderAvailableError(RouterError):
    """Every provider in the chain was filtered out or failed."""


class BudgetExceededError(RouterError):
    """The background budget share (ai.router.background_budget_share) is exhausted for today."""
