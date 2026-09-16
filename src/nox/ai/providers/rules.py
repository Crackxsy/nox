"""RulesProvider: deterministic offline answers, last link of the degradation chain (ADR-008, FR-6).

It never pretends to be a language model: every response is ``degraded=True`` with
``degraded_reason="rules"``. Roles: classify (intent label), chat and background (canned answers).
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime

from nox.ai.base import AiChunk, AiRequest, AiResponse, AiRole, ProviderInfo
from nox.core.events import HealthStatus

PROVIDER_ID = "rules"

_GERMAN_HINTS = re.compile(
    r"\b(ich|du|bist|ist|nicht|und|wie|was|der|die|das|ein|eine|hallo|moin|servus|uhr|bitte|"
    r"danke|geht|dir|mir|heute|jetzt)\b",
    re.IGNORECASE,
)

# Intent rules, checked in order. Each: (intent, compiled regex).
_INTENTS: list[tuple[str, re.Pattern[str]]] = [
    (
        "identity",
        re.compile(
            r"\b(wer|was)\s+bist\s+du\b|\bwie\s+hei(ss|ß)t\s+du\b|\bdein\s+name\b|"
            r"\b(who|what)\s+are\s+you\b|\byour\s+name\b|\bwho\s+is\s+nox\b",
            re.IGNORECASE,
        ),
    ),
    (
        "time",
        re.compile(
            r"\bwie\s+sp(ä|ae)t\b|\buhrzeit\b|\bwie\s+viel\s+uhr\b|\bwelches\s+datum\b|"
            r"\bwelcher\s+tag\b|\bwhat\s+time\b|\btime\s+is\s+it\b|\bwhat('s|\s+is)\s+the\s+date\b|"
            r"\bwhat\s+day\b",
            re.IGNORECASE,
        ),
    ),
    (
        "status",
        re.compile(
            r"\bwie\s+geht('s|\s+es)?(\s+dir)?\b|\balles\s+(ok|okay|gut)\b|\bbist\s+du\s+(da|online|"
            r"wach)\b|\bstatus\b|\bhow\s+are\s+you\b|\bare\s+you\s+(there|ok|okay|online|alive)\b|"
            r"\byou\s+(ok|okay|alive)\b|\bhealth\b",
            re.IGNORECASE,
        ),
    ),
    (
        "greeting",
        re.compile(
            r"^\s*(hallo|hi|hey|moin|servus|hallöchen|huhu|yo|guten\s+(morgen|tag|abend)|"
            r"hello|good\s+(morning|afternoon|evening)|sup|hiya)\b",
            re.IGNORECASE,
        ),
    ),
]

_TEXTS: dict[str, dict[str, str]] = {
    "de": {
        "greeting": "Hey. Ich bin gerade im Offline-Modus und antworte nur regelbasiert, "
        "aber ich bin da.",
        "identity": "Ich bin Nox, dein lokaler Begleiter. Gerade läuft kein Sprachmodell, deshalb "
        "antworte ich nur mit festen Regeln.",
        "status": "Status: Ich laufe, aber kein KI-Provider ist erreichbar. Ich antworte gerade "
        "nur regelbasiert.",
        "time": "Es ist {time} Uhr, {date}.",
        "fallback": "Ich bin gerade offline und kann diese Frage nicht beantworten, aber ich kann "
        "dir die Uhrzeit sagen, meinen Status nennen oder warten, bis wieder ein KI-Modell "
        "erreichbar ist.",
    },
    "en": {
        "greeting": "Hey. I'm in offline mode and only answering with fixed rules right now, "
        "but I'm here.",
        "identity": "I'm Nox, your local companion. No language model is running right now, so "
        "I only answer with fixed rules.",
        "status": "Status: I'm running, but no AI provider is reachable. I'm only answering "
        "rule-based right now.",
        "time": "It's {time}, {date}.",
        "fallback": "I'm offline right now and can't answer that, but I can tell you the time, "
        "report my status, or wait until an AI model is reachable again.",
    },
}

_WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_MONTHS_DE = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def detect_language(text: str, hint: str | None) -> str:
    """``de`` or ``en``: the explicit hint wins, then a stop-word heuristic, default ``de``."""
    if hint in ("de", "en"):
        return hint
    return "de" if _GERMAN_HINTS.search(text) else "en"


def classify_intent(text: str) -> str:
    """Return the rule intent: identity | time | status | greeting | unknown."""
    for intent, pattern in _INTENTS:
        if pattern.search(text):
            return intent
    return "unknown"


class RulesProvider:
    """Always-available deterministic provider. ``clock`` is injectable for tests."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        status_source: Callable[[], Mapping[str, str]] | None = None,
        default_language: str = "de",
    ) -> None:
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._status_source = status_source
        self._default_language = default_language if default_language in ("de", "en") else "de"
        self._info = ProviderInfo(
            id=PROVIDER_ID,
            display_name="Rules (offline fallback)",
            local=True,
            roles=[AiRole.CLASSIFY, AiRole.CHAT, AiRole.BACKGROUND],
            status=HealthStatus.AVAILABLE,
            reason="deterministic rules, always available",
        )

    @property
    def info(self) -> ProviderInfo:
        return self._info

    async def health(self) -> ProviderInfo:
        return self._info

    # -- rendering ------------------------------------------------------------------------------

    def _last_user_text(self, request: AiRequest) -> str:
        for message in reversed(request.messages):
            if message.role == "user":
                return message.content
        return request.messages[-1].content if request.messages else ""

    def _time_answer(self, language: str) -> str:
        now = self._clock()
        if language == "de":
            date = (
                f"{_WEEKDAYS_DE[now.weekday()]}, {now.day}. {_MONTHS_DE[now.month - 1]} {now.year}"
            )
            return _TEXTS["de"]["time"].format(time=now.strftime("%H:%M"), date=date)
        return _TEXTS["en"]["time"].format(
            time=now.strftime("%H:%M"), date=now.strftime("%A, %B %d, %Y")
        )

    def _status_answer(self, language: str) -> str:
        base = _TEXTS[language]["status"]
        if self._status_source is None:
            return base
        extra = ", ".join(f"{k}: {v}" for k, v in sorted(self._status_source().items()))
        return f"{base} ({extra})" if extra else base

    def render(self, request: AiRequest) -> str:
        """Deterministic answer text for ``request`` (also used by the tests)."""
        text = self._last_user_text(request)
        hint = request.metadata.get("language") or self._default_language
        language = detect_language(text, hint if request.metadata.get("language") else None)
        if not request.metadata.get("language"):
            language = detect_language(text, None) if text.strip() else self._default_language
        intent = classify_intent(text)
        if request.role is AiRole.CLASSIFY:
            return intent
        if intent == "time":
            return self._time_answer(language)
        if intent == "status":
            return self._status_answer(language)
        if intent in ("greeting", "identity"):
            return _TEXTS[language][intent]
        return _TEXTS[language]["fallback"]

    # -- AiProvider -----------------------------------------------------------------------------

    async def complete(self, request: AiRequest) -> AiResponse:
        started = time.perf_counter()
        text = self.render(request)
        return AiResponse(
            request_id=request.request_id,
            provider=PROVIDER_ID,
            text=text,
            latency_ms=int((time.perf_counter() - started) * 1000),
            tokens_in=None,
            tokens_out=None,
            degraded=True,
            degraded_reason="rules",
        )

    async def stream(self, request: AiRequest) -> AsyncIterator[AiChunk]:
        text = self.render(request)
        yield AiChunk(request_id=request.request_id, delta=text, done=False)
        yield AiChunk(request_id=request.request_id, delta="", done=True)

    def last_response(self, request_id: str) -> AiResponse | None:
        """Rules responses carry no usage; the router aggregates the streamed text itself."""
        return None
