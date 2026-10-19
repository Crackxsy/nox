"""Deciding whether "I can't do that yet" should become "shall I learn it?".

Learning is offered for exactly one kind of gap: something that is *missing* - no tool for it, or a
curated gap with reason `missing`. Every other answer is better than an offer:

* **Forbidden** (a curated `forbidden` gap, a hard prohibition, a reserved namespace, the home
  boundary, input into a game) - no, and no skill may become a way around it.
* **Disabled / prerequisite** - the capability exists; the concrete fix (enable the plugin, add the
  token) is the answer, not a second, self-built copy of it.
* **Exists** - it is already a usable tool.

The wish itself is also read for the few things that are always refused, so "learn to unlock the
door" is turned down before a session is ever started - the checks would refuse the result anyway,
but nobody should wait ten minutes for that.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from nox.capabilities.model import Gap, GapReason
from nox.security.reserved import name_refusal

__all__ = ["Assessment", "Verdict", "assess"]

#: Wishes that are refused whatever tool name they come with (German and English, lower-case).
_FORBIDDEN_WISHES = re.compile(
    r"rocket league|anti.?cheat|aimbot|\b(cheat|cheats|cheaten|cheating)\b|"
    r"im spiel (dr[üu]ck|klick|steuer|spiel)|"
    r"\b(t[üu]r|haust[üu]r|schloss|garage|garagentor|tor|ventil|alarm\w*)\b.{0,40}"
    r"\b(auf|[öo]ffne\w*|entsperr\w*|aufschlie\w*|deaktivier\w*|unlock\w*|open\w*|disarm\w*)|"
    r"\b(unlock|disarm|open)\b.{0,20}\b(door|lock|garage|gate|alarm|valve)|"
    r"keylogger|passw[öo]rter (auslesen|stehlen)|steal password|read process memory|"
    r"berechtigung\w* (erh[öo]h|umgeh)|bypass (security|permission)",
    re.IGNORECASE,
)


class Verdict(StrEnum):
    OFFER = "offer"
    FIX = "fix"
    REFUSE = "refuse"
    EXISTS = "exists"


@dataclass(frozen=True, slots=True)
class Assessment:
    verdict: Verdict
    message: str


def _matches(name: str, gap: Gap) -> bool:
    gap_name = gap.name.rstrip("*").rstrip(".")
    return bool(gap_name) and (
        name == gap_name or name.startswith(f"{gap_name}.") or gap_name.startswith(f"{name}.")
    )


def assess(
    *,
    capability: str,
    intent: str,
    gaps: Iterable[Gap],
    usable_tools: Iterable[str],
) -> Assessment:
    """What to say when the user wants `intent` and the model believes `capability` is missing."""
    if _FORBIDDEN_WISHES.search(intent):
        return Assessment(
            Verdict.REFUSE,
            "that is outside what Nox may do at all (games, locks, alarms, security), and no "
            "self-built skill can change that",
        )
    name = capability.strip().lower().replace(" ", ".")
    if name:
        refusal = name_refusal(name)
        if refusal is not None:
            return Assessment(Verdict.REFUSE, f"{capability!r} {refusal}; that is not learnable")
        if name in set(usable_tools):
            return Assessment(Verdict.EXISTS, f"{name} already exists and can be used directly")
        for gap in gaps:
            if not _matches(name, gap):
                continue
            if gap.reason is GapReason.FORBIDDEN:
                return Assessment(Verdict.REFUSE, f"{gap.what}: {gap.detail}")
            if gap.reason in (GapReason.DISABLED, GapReason.PREREQUISITE):
                return Assessment(Verdict.FIX, f"{gap.what} - {gap.detail}")
    return Assessment(Verdict.OFFER, "Kann ich noch nicht - soll ich es mir beibringen?")
