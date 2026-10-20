"""Saying on stream that Nox's voice is AI-generated.

The Supertonic 3 weights (Nox's default voice) are licensed under OpenRAIL-M, which requires
disclosing machine-generated audio - and viewers deserve to know with any synthetic voice. So, by
default (`stream.ai_voice_disclosure`):

* once per stream session, when `stream.started` arrives, one short line goes to the Twitch chat -
  through the `ToolExecutor` (`twitch.chat.send` in stream mode), never by talking to the plugin
  directly. A second `stream.started` within the same session (OBS or the plugin reconnecting)
  posts nothing; `stream.ended` resets it. A send that fails - the chat not connected yet, the
  permission engine saying no - is logged and tried once more when the Twitch chat connects
  during the same session. Nothing here ever raises into the bus.
* a small label in a corner of the OBS overlay: `system.ai_voice_label` in the core state, set
  once at start from the configuration, which the overlay reads like the rest of its public state
  (`nox.ipc.pet_view`). The desktop pet does not show it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nox.core.config import NoxConfig, StreamAiVoiceDisclosureConfig
from nox.core.events import E, Event, EventBus
from nox.core.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "AGENT",
    "LABEL_PATH",
    "AiVoiceDisclosure",
    "ai_voice_label",
    "default_chat_text",
    "disclosure_text",
]

#: The agent name the core's own stream actions use towards the permission engine.
AGENT = "nox.stream"
#: The state path the overlay reads its label from.
LABEL_PATH = "system.ai_voice_label"
#: Sends per stream session: the one at `stream.started`, and one more when the chat connects.
MAX_ATTEMPTS = 2

_DEFAULT_TEXTS: dict[str, str] = {
    "de": "Kurzer Hinweis: Die Stimme von {name} ist KI-generiert.",
    "en": "Quick note: {name}'s voice is AI-generated.",
}


def _language(config: NoxConfig) -> str:
    speech = str(config.identity.speech_language)
    if speech in _DEFAULT_TEXTS:
        return speech
    ui = str(config.identity.ui_language)
    return ui if ui in _DEFAULT_TEXTS else "de"


def default_chat_text(name: str, language: str) -> str:
    template = _DEFAULT_TEXTS.get(language, _DEFAULT_TEXTS["en"])
    return template.format(name=name.strip() or "Nox")


def disclosure_text(config: NoxConfig) -> str:
    """The chat line: the configured one, or the built-in sentence in Nox's language. Empty when
    the disclosure is off."""
    settings = config.stream.ai_voice_disclosure
    if not settings.enabled:
        return ""
    custom = settings.chat_text.strip()
    if custom:
        return custom
    return default_chat_text(str(config.identity.name), _language(config))


def ai_voice_label(settings: StreamAiVoiceDisclosureConfig) -> str:
    """The overlay label; empty (no label) when the disclosure is off or the label is blank."""
    return settings.overlay_label.strip() if settings.enabled else ""


class AiVoiceDisclosure:
    """Posts the chat line once per stream session and publishes the overlay label."""

    def __init__(
        self,
        bus: EventBus,
        executor: Any,
        *,
        text: Callable[[], str],
        label: Callable[[], str],
        state: Any = None,
    ) -> None:
        self._bus = bus
        self._executor = executor
        self._text = text
        self._label = label
        self._state = state
        self._unsubs: list[Callable[[], None]] = []
        self._session: str | None = None  # the stream session that is running, if any
        self._posted = False
        self._attempts = 0  # sends tried in the current session

    def start(self) -> None:
        self._unsubs = [
            self._bus.subscribe(E.STREAM_STARTED, self._on_started),
            self._bus.subscribe(E.STREAM_ENDED, self._on_ended),
            self._bus.subscribe(E.TWITCH_CONNECTED, self._on_twitch_connected),
        ]

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []

    async def publish_label(self) -> None:
        """Set `system.ai_voice_label` from the configuration (at start; never raises)."""
        if self._state is None:
            return
        try:
            await self._state.update(LABEL_PATH, self._label(), reason="config")
        except Exception as exc:  # noqa: BLE001 - a missing label must not break the boot
            log.warning("stream.ai_voice_label_failed", error=f"{type(exc).__name__}: {exc}")

    async def _on_started(self, ev: Event) -> None:
        session = str(ev.payload.get("session_id", "")) or "unknown"
        if self._session is not None:
            # Still the same stream as far as Nox knows (no `stream.ended` in between): a
            # reconnect announcing itself again. Said once is enough.
            log.info("stream.ai_voice_disclosure_skipped", reason="same_session")
            return
        self._session = session
        self._posted = False
        self._attempts = 0
        await self._post()

    async def _on_ended(self, _ev: Event) -> None:
        self._session = None
        self._posted = False
        self._attempts = 0

    async def _on_twitch_connected(self, _ev: Event) -> None:
        if self._session is not None and not self._posted:
            await self._post()

    async def _post(self) -> None:
        text = self._text()
        if not text:
            self._posted = True  # disabled: nothing is owed for this session
            return
        if self._attempts >= MAX_ATTEMPTS:
            return
        self._attempts += 1
        try:
            result = await self._executor.call(
                agent=AGENT, name="twitch.chat.send", arguments={"text": text}, mode="stream"
            )
        except Exception as exc:  # noqa: BLE001 - a bus handler never raises
            log.warning("stream.ai_voice_disclosure_failed", error=f"{type(exc).__name__}: {exc}")
            return
        if getattr(result, "ok", False):
            self._posted = True
            log.info("stream.ai_voice_disclosure_posted", session=self._session)
        else:
            log.warning(
                "stream.ai_voice_disclosure_failed", error=str(getattr(result, "error", "") or "")
            )
