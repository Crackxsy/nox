"""`RemoteService`: turns `remote.message` events from the transport plugin into policy decisions,
executes the allowed ones and answers on the same channel.

Everything a phone can reach goes through here, and everything that happens is audited twice: once
in the hash-chained security audit (`AuditLog`) and once in `remote_audit` with
the command verb only. What a phone can reach is deliberately small:

  /status           read-only snapshot - mode, privacy mode, safe mode, pet state. Never a
                    transcript, a memory entry, a chat history or a secret.
  /kill             `KillSwitchService.engage("telegram",...)`. `telegram` is not in
                    `SECURITY_PATH_ORIGINS`, so this is a *user* kill: resuming needs no PIN - but
                    resume is not offered here at all and `/resume` is denied explicitly, because
                    the resume roles (`shell`, `dashboard`, `supervisor`) never include `remote`.
  /privacy private|offline   allowed. Widening back to `full` from a phone is always denied.
  /pair <code>      redeem a one-time code shown locally; /unpair revokes the calling device.
  anything else     chat, routed through the normal orchestrator with `speak=False` and the same
                    personality/prompt path as the desktop - no separate or relaxed profile.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from nox.core.events import E, Event, EventBus, RemoteMessage
from nox.core.logging import get_logger
from nox.core.state import PrivacyMode
from nox.remote.models import DeviceRow, RemoteDecision
from nox.remote.pairing import PairingService
from nox.remote.policy import RemoteCommandPolicy
from nox.remote.repo import RemoteRepository
from nox.security.model import AuditLog

log = get_logger(__name__)

#: The kill-switch origin a phone command carries. Deliberately not in `SECURITY_PATH_ORIGINS`.
REMOTE_KILL_ORIGIN = "telegram"

Sender = Callable[[str, str], Awaitable[None]]
StatusProvider = Callable[[], Mapping[str, str]]
ChatHandler = Callable[[str], Awaitable[str]]


class KillSwitchLike(Protocol):
    """The slice of `nox.security.killswitch.KillSwitchService` this service uses."""

    async def engage(self, origin: str, reason: str = "") -> object: ...


class PrivacyController(Protocol):
    """The slice of `nox.security.privacy.PrivacyService` this service uses.

    Declared as a Protocol rather than resolved with `getattr` so that a wiring mistake is a type
    error at build time instead of a phone being told its privacy mode changed when it did not.
    """

    async def set_mode(
        self, mode: PrivacyMode, *, by: str = "user", confirmed: bool = False
    ) -> Any: ...


_DENY_TEXT = {
    "not_paired": "Dieses Gerät ist nicht gekoppelt.",
    "already_paired": "Dieses Gerät ist bereits gekoppelt.",
    "unknown_command": "Unbekannter Befehl. Verfügbar: /status /kill /privacy /pair /unpair",
    "replay": "Nachricht abgelehnt (bereits verarbeitet).",
    "rate_limited": "Zu viele Befehle. Bitte kurz warten.",
    "resume_not_remote": "Fortsetzen geht nur lokal am Rechner, nicht vom Telefon.",
    "privacy_upgrade_denied": "Vom Telefon aus nur /privacy private oder /privacy offline.",
    "privacy_mode_missing": "Nutzung: /privacy private|offline",
    "chat_disabled": "Chat über das Telefon ist deaktiviert.",
    "empty_message": "Leere Nachricht.",
}

_HELP_TEXT = (
    "Befehle: /status, /kill, /privacy private|offline, /pair <code>, /unpair. "
    "Alles andere ist ein normaler Chat mit Nox."
)


class RemoteService:
    def __init__(
        self,
        *,
        repo: RemoteRepository,
        pairing: PairingService,
        policy: RemoteCommandPolicy,
        killswitch: KillSwitchLike,
        privacy: PrivacyController,
        send: Sender,
        status: StatusProvider,
        chat: ChatHandler | None = None,
        bus: EventBus | None = None,
        audit: AuditLog | None = None,
        channel: str = "telegram",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._pairing = pairing
        self._policy = policy
        self._killswitch = killswitch
        self._privacy = privacy
        self._send = send
        self._status = status
        self._chat = chat
        self._bus = bus
        self._audit = audit
        self._channel = channel
        self._clock = clock or (lambda: datetime.now(UTC))
        self._unsub: Callable[[], None] | None = None

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        if self._bus is not None and self._unsub is None:
            self._unsub = self._bus.subscribe(E.REMOTE_MESSAGE, self._on_message)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    async def _on_message(self, event: Event) -> None:
        try:
            message = RemoteMessage.model_validate(event.payload)
        except ValueError as exc:
            log.warning("remote.message_invalid", error=str(exc))
            return
        await self.handle(message)

    # -- the one entry point ---------------------------------------------------------------------

    async def handle(self, message: RemoteMessage) -> RemoteDecision:
        device = self._repo.find_device_for_sender(self._channel, message.sender_id)
        decision = self._policy.decide(message, device)
        await self._record(message, decision)
        if not decision.allowed:
            # An unpaired sender is ignored on the wire: it is audited, and it gets no answer at
            # all, so the bot cannot be used to probe whether a Nox instance is listening.
            if decision.reason != "not_paired":
                await self._reply(message, _DENY_TEXT.get(decision.reason, "Abgelehnt."))
            return decision

        if device is not None and message.update_id:
            self._repo.touch_device(device.id, when=self._clock(), update_id=message.update_id)
        await self._execute(message, decision, device)
        return decision

    async def _execute(
        self, message: RemoteMessage, decision: RemoteDecision, device: DeviceRow | None
    ) -> None:
        command = decision.command
        if command == "pair":
            code = decision.args[0] if decision.args else ""
            result = await self._pairing.redeem(code, sender_id=message.sender_id)
            text = (
                f"Gekoppelt als „{result.name}“."
                if result.ok
                else f"Kopplung abgelehnt ({result.reason})."
            )
            await self._reply(message, text)
            return
        if device is None:  # pragma: no cover - the policy already rejected this
            return
        if command == "unpair":
            await self._pairing.revoke(device.id, reason="phone")
            await self._reply(message, "Gerät entkoppelt. Weitere Befehle werden abgelehnt.")
            return
        if command == "status":
            await self._reply(message, self._status_text())
            return
        if command == "help":
            await self._reply(message, _HELP_TEXT)
            return
        if command == "kill":
            await self._killswitch.engage(REMOTE_KILL_ORIGIN, "kill switch from paired phone")
            await self._reply(
                message,
                "Not-Aus aktiv. Fortsetzen geht nur lokal am Rechner (ohne PIN, aber nicht "
                "von hier).",
            )
            return
        if command == "privacy":
            await self._reply(message, await self._set_privacy(decision.args[0].lower()))
            return
        if command == "chat":
            await self._handle_chat(message, decision.args[0])

    async def _handle_chat(self, message: RemoteMessage, text: str) -> None:
        if self._chat is None:
            await self._reply(message, "Chat ist gerade nicht verfügbar.")
            return
        try:
            answer = await self._chat(text)
        except Exception as exc:  # noqa: BLE001 - an AI failure must not kill the bus handler
            log.warning("remote.chat_failed", error=type(exc).__name__)
            await self._reply(message, "Antwort nicht möglich (KI nicht verfügbar).")
            return
        await self._reply(message, answer or "(keine Antwort)")

    async def _set_privacy(self, mode: str) -> str:
        """Switch the privacy mode and report what actually happened.

        A mode change that raised is reported as a failure; the phone is never told a security
        control moved when it did not.
        """
        try:
            change = await self._privacy.set_mode(PrivacyMode(mode), by=REMOTE_KILL_ORIGIN)
        except ValueError:
            return "Unbekannter Modus. Nutzung: /privacy private|offline"
        except Exception as exc:  # noqa: BLE001 - reported to the phone, never swallowed
            log.error("remote.privacy_change_failed", mode=mode, error=str(exc))
            return "Privatsphäre-Modus nicht geändert (Umschalten nicht möglich)."
        current = getattr(change, "current", None)
        applied = str(getattr(current, "value", current or mode))
        return f"Privatsphäre-Modus: {applied}."

    def _status_text(self) -> str:
        """Allow-list, not a denylist: only the keys the status provider hands over are rendered,
        and that provider is wired in `install.py` to plain scalars (mode, privacy mode, safe mode,
        pet state, uptime). No transcript, memory or chat content can reach this string."""
        values = self._status()
        return "\n".join(f"{key}: {value}" for key, value in values.items()) or "keine Daten"

    # -- audit + reply ---------------------------------------------------------------------------

    async def _record(self, message: RemoteMessage, decision: RemoteDecision) -> None:
        self._repo.append_audit(
            ts=self._clock(),
            channel=self._channel,
            sender_id=message.sender_id,
            device_id=decision.device_id,
            command=decision.command,
            decision="allow" if decision.allowed else "deny",
            reason=decision.reason,
        )
        if self._audit is not None:
            self._audit.append(
                actor="remote",
                tool="remote",
                action=decision.command,
                target=decision.device_id,
                decision="allow" if decision.allowed else "deny",
                result="ok" if decision.allowed else decision.reason,
                details={"channel": self._channel},
            )
        if self._bus is not None:
            await self._bus.publish(
                Event(
                    name=E.REMOTE_COMMAND,
                    payload={
                        "channel": self._channel,
                        "sender_id": message.sender_id,
                        "device_id": decision.device_id,
                        "command": decision.command,
                        "allowed": decision.allowed,
                        "reason": decision.reason,
                    },
                )
            )
        log.info(
            "remote.command",
            command=decision.command,
            allowed=decision.allowed,
            reason=decision.reason,
        )

    async def _reply(self, message: RemoteMessage, text: str) -> None:
        try:
            await self._send(message.chat_id, text)
        except Exception as exc:  # noqa: BLE001 - a failed send must not tear down the bus handler
            log.warning("remote.reply_failed", error=type(exc).__name__)
