"""Remote command policy. Pure decision logic: given an inbound `remote.message` it says which
command
was asked for and whether this sender may have it. Executing the decision is `RemoteService`'s job,
so every rule here is unit-testable without a kill switch, a database or a transport.

The rules, in the order they are applied:
1. unknown/unsupported verb -> deny `unknown_command` 2. `/pair <code>` from any sender -> allow
(the only command an unpaired sender has) 3. any other command from an unpaired sender -> deny
`not_paired` (and it is audited) 4. stale or reused `update_id` -> deny `replay` 5. sender over its
command budget -> deny `rate_limited` 6. `/resume` -> deny `resume_not_remote`, always. Resume
stays local-only (Security Model: `remote` is never an accepted resume origin), so it is a named,
audited denial rather than an unknown verb. 7. `/privacy <mode>`: `private`/`offline` allow,
anything else (including back to `full`) -> deny `privacy_upgrade_denied`. Conservative and pending
approval: a phone may make Nox more private, never less (Spec, Security Model). 8. `/kill`,
`/status`, `/unpair`, plain chat -> allow for a paired sender.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from nox.core.events import RemoteMessage
from nox.remote.models import DeviceRow, RemoteDecision

#: Every verb the remote surface understands. Anything else is `unknown_command`, never chat - a
#: mistyped command must not be quietly forwarded to the AI as conversation.
COMMANDS: frozenset[str] = frozenset(
    {"status", "kill", "privacy", "pair", "unpair", "resume", "help", "chat"}
)

#: Privacy modes a phone may switch *to*. `full`/`balanced` are absent on purpose (rule 7).
REMOTE_PRIVACY_MODES: frozenset[str] = frozenset({"private", "offline"})

#: Commands an unpaired sender may use at all.
UNPAIRED_COMMANDS: frozenset[str] = frozenset({"pair"})


def parse_command(text: str) -> tuple[str, list[str]]:
    """`"/privacy private"` -> `("privacy", ["private"])`; anything not starting with `/` is chat.

    A `/cmd@botname` suffix (what Telegram appends in groups) is stripped so the same verb table
    works in both. An empty message is `("chat", [])` and gets rejected further down as empty.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "chat", [stripped] if stripped else []
    parts = stripped[1:].split()
    if not parts:
        return "unknown", []
    verb = parts[0].split("@", 1)[0].lower()
    return (verb if verb in COMMANDS else "unknown"), parts[1:]


class RemoteRateLimiter:
    """Per-sender command budget (Spec: tighter than any local client). Token bucket: at most
    `burst` commands back to back, refilled at `per_minute`/60 per second."""

    def __init__(
        self,
        *,
        per_minute: int = 20,
        burst: int = 5,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._rate = per_minute / 60.0
        self._burst = float(burst)
        self._clock = clock or time.monotonic
        self._tokens: dict[str, tuple[float, float]] = {}

    def try_acquire(self, key: str) -> bool:
        now = self._clock()
        tokens, last = self._tokens.get(key, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens < 1.0:
            self._tokens[key] = (tokens, now)
            return False
        self._tokens[key] = (tokens - 1.0, now)
        return True


class RemoteCommandPolicy:
    def __init__(
        self,
        *,
        rate_limiter: RemoteRateLimiter | None = None,
        chat_enabled: bool = True,
    ) -> None:
        self._rate = rate_limiter or RemoteRateLimiter()
        self._chat_enabled = chat_enabled

    def decide(self, message: RemoteMessage, device: DeviceRow | None) -> RemoteDecision:
        command, args = parse_command(message.text)
        device_id = device.id if device is not None else ""

        def deny(reason: str) -> RemoteDecision:
            return RemoteDecision(
                allowed=False, command=command, reason=reason, device_id=device_id, args=args
            )

        if command == "unknown":
            return deny("unknown_command")
        if device is None:
            if command not in UNPAIRED_COMMANDS:
                # Rule 3: an unpaired sender is ignored - but never silently (Spec).
                return deny("not_paired")
            if not self._rate.try_acquire(f"{message.channel}:{message.sender_id}"):
                return deny("rate_limited")
            return RemoteDecision(allowed=True, command=command, args=args)

        if command == "pair":
            return deny("already_paired")
        # Rule 4: the transport's own sequence number must strictly advance. A captured message
        # replayed later carries an `update_id` we have already seen.
        if message.update_id and message.update_id <= device.last_update_id:
            return deny("replay")
        if not self._rate.try_acquire(f"{message.channel}:{device.id}"):
            return deny("rate_limited")
        if command == "resume":
            return deny("resume_not_remote")
        if command == "privacy":
            mode = (args[0].lower() if args else "").strip()
            if not mode:
                return deny("privacy_mode_missing")
            if mode not in REMOTE_PRIVACY_MODES:
                return deny("privacy_upgrade_denied")
        if command == "chat":
            if not self._chat_enabled:
                return deny("chat_disabled")
            if not args or not args[0]:
                return deny("empty_message")

        return RemoteDecision(allowed=True, command=command, device_id=device.id, args=args)
