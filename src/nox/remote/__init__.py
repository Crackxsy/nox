"""Mobile Companion core side (Spec v0.8 Mobile Companion, EPIC-17): pairing, the remote command
policy, notification gating and the IPC surface the dashboard's "Remote" page talks to.

The transport lives in `plugins/telegram`; nothing in this package knows about the Bot API. The
plugin turns inbound messages into `remote.message` events and exposes exactly one outbound tool
(`telegram.send`) - every trust decision (is this sender paired? may it kill? may it switch privacy
mode?) is made here, below the AI layer, per Security Model §2 and ENGINEERING.md's hard rules.
"""

from __future__ import annotations

from nox.remote.models import DeviceRow, PairingResult, PairingStart, RemoteDecision
from nox.remote.notify import RemoteNotifier
from nox.remote.pairing import PairingService
from nox.remote.policy import RemoteCommandPolicy, RemoteRateLimiter, parse_command
from nox.remote.repo import RemoteRepository
from nox.remote.service import RemoteService

__all__ = [
    "DeviceRow",
    "PairingResult",
    "PairingService",
    "PairingStart",
    "RemoteCommandPolicy",
    "RemoteDecision",
    "RemoteNotifier",
    "RemoteRateLimiter",
    "RemoteRepository",
    "RemoteService",
    "parse_command",
]
