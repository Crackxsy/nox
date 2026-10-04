"""The start greeting says when Nox is not answering with its best backend, and how to fix it.

From the product owner's first real evening: Claude Code's login had expired, the small local
model answered everything, and Nox still greeted with "Ich bin bereit."
"""

from __future__ import annotations

from dataclasses import dataclass

from nox.core.boot.greeting import GREETING, greeting_text
from nox.core.events import HealthStatus

CHAIN = ["claude_code", "ollama", "rules"]


@dataclass
class _H:
    status: HealthStatus
    reason: str = ""


def test_all_well_is_the_plain_greeting() -> None:
    health = {"ai.claude_code": _H(HealthStatus.AVAILABLE), "ai.ollama": _H(HealthStatus.AVAILABLE)}
    assert greeting_text("de", health, CHAIN) == GREETING["de"]


def test_an_expired_login_is_named_with_what_fixes_it() -> None:
    health = {
        "ai.claude_code": _H(
            HealthStatus.UNAVAILABLE,
            "2.1.288 (Claude Code); Failed to authenticate: OAuth session expired",
        ),
        "ai.ollama": _H(HealthStatus.AVAILABLE),
    }
    text = greeting_text("de", health, CHAIN)
    assert "Claude ist gerade nicht erreichbar" in text
    assert "über das lokale Modell" in text
    assert "Anmeldung ist abgelaufen" in text and "slash login" in text


def test_an_unknown_reason_is_not_paraphrased_into_advice() -> None:
    health = {
        "ai.claude_code": _H(HealthStatus.UNAVAILABLE, "something nobody anticipated"),
        "ai.ollama": _H(HealthStatus.AVAILABLE),
    }
    text = greeting_text("de", health, CHAIN)
    assert text.endswith("das kann einfacher klingen.")


def test_a_backend_not_probed_yet_is_not_called_down() -> None:
    assert greeting_text("de", {}, CHAIN) == GREETING["de"]


def test_english_follows_the_language() -> None:
    health = {
        "ai.claude_code": _H(HealthStatus.UNAVAILABLE, "timeout after 15s"),
        "ai.ollama": _H(HealthStatus.AVAILABLE),
    }
    assert greeting_text("en", health, CHAIN) == (
        "Hi, I am Nox. Claude is not available right now, so the local model is answering for "
        "now - it may sound simpler. It cannot be reached right now."
    )
