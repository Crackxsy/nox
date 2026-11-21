"""Exceptions of the AI layer: providers raise ProviderError, the router RouterError."""

from __future__ import annotations

from enum import StrEnum

from nox.ai.limits import ProviderLimit


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


class ProviderLimitedError(ProviderUnavailableError):
    """The provider's usage limit is used up until `limit.until`. Retrying sooner only fails again,
    so the router keeps the provider out of the chain until then instead of for a minute."""

    def __init__(self, provider: str, message: str, limit: ProviderLimit) -> None:
        super().__init__(provider, message)
        self.limit = limit


class RouterError(Exception):
    """The router could not produce a response."""


class NoProviderCause(StrEnum):
    """Why a request found no provider, in the terms a user can act on."""

    #: A provider's plan limit is used up (`NoProviderAvailableError.limit` says until when).
    LIMIT = "limit"
    #: Every provider that could answer is cloud-based and the privacy mode or profile forbids it.
    PRIVACY = "privacy"
    #: The background share of the cloud budget is spent.
    BUDGET = "budget"
    #: The providers that were tried did not answer in time.
    TIMEOUT = "timeout"
    #: The providers that were tried failed.
    FAILED = "failed"
    #: No provider is configured, signed in or running that serves this request.
    UNAVAILABLE = "unavailable"


class NoProviderAvailableError(RouterError):
    """Every provider in the chain was filtered out or failed. `cause` says which of the cases a
    user can tell apart it was; `limit` is set for `NoProviderCause.LIMIT`."""

    def __init__(
        self,
        message: str,
        *,
        cause: NoProviderCause = NoProviderCause.UNAVAILABLE,
        limit: ProviderLimit | None = None,
    ) -> None:
        super().__init__(message)
        self.cause = cause
        self.limit = limit


class BudgetExceededError(RouterError):
    """The background budget share (ai.router.background_budget_share) is exhausted for today."""
