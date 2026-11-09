"""obs-websocket v5 client - kept in `nox.stream.obs_ws`, which a `nox node` with OBS uses too.

Re-exported here so the plugin (and its tests) keep importing it from where they always did.
"""

from __future__ import annotations

from nox.stream.obs_ws import (
    CLOSE_AUTHENTICATION_FAILED,
    DEFAULT_EVENT_SUBSCRIPTIONS,
    EVENT_SUB_GENERAL,
    EVENT_SUB_OUTPUTS,
    EVENT_SUB_SCENES,
    OP_EVENT,
    OP_HELLO,
    OP_IDENTIFIED,
    OP_IDENTIFY,
    OP_REQUEST,
    OP_REQUEST_RESPONSE,
    RPC_VERSION,
    AuthFailedHook,
    ConnectedHook,
    DisconnectedHook,
    EventHandler,
    ObsAuthFailedError,
    ObsAuthRequiredError,
    ObsProtocolError,
    ObsRequestError,
    ObsWebSocketClient,
    PasswordProvider,
    WebSocketLike,
    compute_auth_string,
)

__all__ = [
    "CLOSE_AUTHENTICATION_FAILED",
    "DEFAULT_EVENT_SUBSCRIPTIONS",
    "EVENT_SUB_GENERAL",
    "EVENT_SUB_OUTPUTS",
    "EVENT_SUB_SCENES",
    "OP_EVENT",
    "OP_HELLO",
    "OP_IDENTIFIED",
    "OP_IDENTIFY",
    "OP_REQUEST",
    "OP_REQUEST_RESPONSE",
    "RPC_VERSION",
    "AuthFailedHook",
    "ConnectedHook",
    "DisconnectedHook",
    "EventHandler",
    "ObsAuthFailedError",
    "ObsAuthRequiredError",
    "ObsProtocolError",
    "ObsRequestError",
    "ObsWebSocketClient",
    "PasswordProvider",
    "WebSocketLike",
    "compute_auth_string",
]
