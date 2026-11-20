"""The core's watched games, sent to the nodes for their game lock.

A node that finds one of these processes running keeps Nox's hands off the machine while it plays.
The list is sent on every connection and again when the settings change; a node only ever adds to
what it was told, never removes (an addition there, never a removal).
"""

from __future__ import annotations

from nox.core.config import NoxConfig
from nox.core.logging import get_logger
from nox.mcp.client import McpClientError
from nox.node.hub.listener import NodeListener, NodeSession

log = get_logger(__name__)

__all__ = ["resend_games_if_changed", "send_games", "watched_games"]

#: How many process names one message to a node carries at most.
MAX_GAMES = 64


def watched_games(config: NoxConfig) -> list[str]:
    """The process names of the games the core watches."""
    return list(config.sensors.game.process_names)[:MAX_GAMES]


async def send_games(config: NoxConfig, session: NodeSession) -> None:
    """The core's watched games, for the node's game lock - an addition there, never a removal."""
    names = watched_games(config)
    if not names:
        return
    try:
        await session.client.request("nox/games", {"add": names}, timeout_s=5)
    except (TimeoutError, McpClientError) as exc:
        log.warning("node.games_failed", device_id=session.device_id, error=str(exc))


async def resend_games_if_changed(
    config: NoxConfig, listener: NodeListener, sent: list[list[str]]
) -> bool:
    """The core's watched games changed (settings): every connected node gets them again (the
    node only ever adds). True when they were sent."""
    games = watched_games(config)
    if sent and sent[0] == games:
        return False
    sent[:] = [games]
    for session in list(listener.sessions.values()):
        await send_games(config, session)
    return True
