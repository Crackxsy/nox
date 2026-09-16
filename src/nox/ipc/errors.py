"""IPC error codes and the IpcError exception shared by hub, dispatcher and client (IPC Model)."""

from __future__ import annotations

from typing import Any

from nox.ipc.protocol import ErrorPayload, Source

ERR_AUTH_DENIED = "auth.denied"
ERR_VALIDATION = "validation.failed"
ERR_PERMISSION = "permission.denied"
ERR_CONFIRM_REQUIRED = "permission.confirm_required"
ERR_NOT_FOUND = "not_found"
ERR_TIMEOUT = "timeout"
ERR_UNAVAILABLE = "unavailable"
ERR_INTERNAL = "internal"
ERR_RATE_LIMITED = "rate_limited"

CORE_SOURCE = Source(role="core", id="core")


class IpcError(Exception):
    """Typed IPC failure. Handlers raise it to answer with an error frame; clients raise it too."""

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> IpcError:
        try:
            parsed = ErrorPayload.model_validate(payload)
        except ValueError:
            return cls(
                ERR_INTERNAL, "malformed error payload", details={"raw_keys": sorted(payload)}
            )
        return cls(parsed.code, parsed.message, retryable=parsed.retryable, details=parsed.details)

    def to_payload(self) -> dict[str, Any]:
        return ErrorPayload(
            code=self.code, message=self.message, retryable=self.retryable, details=self.details
        ).model_dump(mode="json")

    def __repr__(self) -> str:
        return f"IpcError(code={self.code!r}, message={self.message!r})"
