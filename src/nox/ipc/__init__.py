"""nox.ipc: loopback WebSocket hub, client, HTTP endpoints and tokens (IPC Model, ADR-003)."""

from nox.ipc.client import InboundRequest, IpcClient, IpcClientThread, StreamCall
from nox.ipc.dispatch import (
    ROLE_ALLOWLIST,
    EmptyPayload,
    RequestContext,
    RequestRegistry,
    match_name,
    role_allows,
)
from nox.ipc.errors import (
    ERR_AUTH_DENIED,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_PERMISSION,
    ERR_RATE_LIMITED,
    ERR_TIMEOUT,
    ERR_UNAVAILABLE,
    ERR_VALIDATION,
    IpcError,
)
from nox.ipc.http import HttpServer, HttpSettings, create_app
from nox.ipc.protocol import (
    SCHEMA_VERSION,
    AuthRequest,
    AuthResponse,
    Envelope,
    ErrorPayload,
    Kind,
    Source,
)
from nox.ipc.server import ClientInfo, HubSettings, IpcHub, read_runtime_info, role_may_see
from nox.ipc.tokens import TokenStore, generate_token, read_session_token

__all__ = [
    "ERR_AUTH_DENIED",
    "ERR_INTERNAL",
    "ERR_NOT_FOUND",
    "ERR_PERMISSION",
    "ERR_RATE_LIMITED",
    "ERR_TIMEOUT",
    "ERR_UNAVAILABLE",
    "ERR_VALIDATION",
    "ROLE_ALLOWLIST",
    "SCHEMA_VERSION",
    "AuthRequest",
    "AuthResponse",
    "ClientInfo",
    "EmptyPayload",
    "Envelope",
    "ErrorPayload",
    "HttpServer",
    "HttpSettings",
    "HubSettings",
    "InboundRequest",
    "IpcClient",
    "IpcClientThread",
    "IpcError",
    "IpcHub",
    "Kind",
    "RequestContext",
    "RequestRegistry",
    "Source",
    "StreamCall",
    "TokenStore",
    "create_app",
    "generate_token",
    "match_name",
    "read_runtime_info",
    "read_session_token",
    "role_allows",
    "role_may_see",
]
