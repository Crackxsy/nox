"""What Nox learns about the user from talking to them, and how it is kept in front of the model.

Asked "was weißt du über mich?", Nox answered "ehrlich gesagt: kaum etwas" - after weeks of use.
Nothing was wrong with the memory store; nothing ever wrote to it unless the user said "merk dir".
Two pieces fix that:

* :class:`FactCapture` watches finished conversation turns. Once the conversation has gone quiet
  for `settle_s` (or every `batch_turns` turns), it asks a model to pull the durable facts about
  the user out of the recent exchanges - people, likes, games, projects, dated plans - and stores
  each one as a memory item, unless a near-duplicate is already known. It runs in the background
  and never delays an answer. It extracts nothing while the privacy mode forbids memory writes,
  because the memory service refuses the write and the exchange is simply dropped.
* :class:`UserProfile` renders those facts as a block for the system prompt, so the model knows
  them without having to search for them. Retrieval still exists for everything else; this is the
  part that should never depend on a similarity score.

Facts are stored in the language they were said in and in the third person, so they read the
same in a prompt and on the dashboard, where the user can delete any of them.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from nox.ai.base import AiRequest, AiResponse, AiRole, Message
from nox.core.logging import get_logger
from nox.data.repos import MemoryItemRepository, MemoryItemRow
from nox.memory.embeddings import EmbeddingService
from nox.memory.importance import MemoryType
from nox.memory.items import MemoryService, MemoryWriteRefusedError

log = get_logger(__name__)

__all__ = [
    "PROFILE_TYPES",
    "CapturedFact",
    "FactCapture",
    "UserProfile",
    "extract_facts_json",
    "normalise_fact",
]

#: The memory types that describe the user rather than a task, a stream or Nox itself. These are
#: what the profile block shows and what capture may write.
PROFILE_TYPES: tuple[MemoryType, ...] = (
    MemoryType.USER,
    MemoryType.PREFERENCE,
    MemoryType.RELATIONSHIP,
    MemoryType.EVENT,
    MemoryType.TASK,
)

#: Cosine similarity above which a candidate is the same fact as one already stored.
DUPLICATE_SCORE = 0.92

#: Longest fact the model may hand back; anything longer is a paragraph, not a fact.
MAX_FACT_CHARS = 200

_SOURCE = "capture"
_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^\w\s]")


Complete = Callable[[AiRequest], Awaitable[AiResponse]]


@dataclass(frozen=True, slots=True)
class CapturedFact:
    text: str
    type: MemoryType
    confidence: float


def normalise_fact(text: str) -> str:
    """Lower-case, no punctuation, single spaces: two spellings of one fact compare equal."""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", text.lower())).strip()


def extract_facts_json(text: str) -> list[CapturedFact]:
    """The facts in a model answer. Tolerates prose around the JSON array and skips bad entries.

    A model that answers with nothing usable yields an empty list, never an exception: capture is
    a background nicety and must not become a background error.
    """
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        raw = json.loads(text[start : end + 1])
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    facts: list[CapturedFact] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        fact_text = str(entry.get("text", "")).strip()
        if not fact_text or len(fact_text) > MAX_FACT_CHARS:
            continue
        try:
            kind = MemoryType(str(entry.get("type", MemoryType.USER.value)))
        except ValueError:
            kind = MemoryType.USER
        if kind not in PROFILE_TYPES:
            kind = MemoryType.USER
        try:
            confidence = float(entry.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        facts.append(CapturedFact(fact_text, kind, min(max(confidence, 0.0), 1.0)))
    return facts


# ---- the profile block -------------------------------------------------------------------------


class UserProfile:
    """The facts about the user, as the prompt and the dashboard need them."""

    def __init__(
        self, repo: MemoryItemRepository, *, user_name: str, max_chars: int = 1500
    ) -> None:
        self._repo = repo
        self._user_name = user_name
        self._max_chars = max_chars

    def items(self, limit: int = 200) -> list[MemoryItemRow]:
        """Most important first, then newest first."""
        return self._repo.list_by_types([t.value for t in PROFILE_TYPES], limit=limit)

    def block(self) -> str:
        """The system-prompt section, or an empty string when nothing is known yet.

        Bounded by `max_chars` so a long-lived profile cannot crowd out the question.
        """
        rows = self.items()
        if not rows:
            return ""
        head = (
            f"What you know about {self._user_name} (learned in earlier conversations; use it "
            "naturally when it is relevant, do not recite it unprompted):"
        )
        lines = [head]
        used = len(head)
        for row in rows:
            line = f"- ({row.type}, {row.created_at:%Y-%m-%d}) {row.text}"
            if used + len(line) > self._max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)


# ---- capture -----------------------------------------------------------------------------------


_EXTRACTION_SYSTEM = """\
You maintain what an assistant called Nox knows about its user, {user}.
From the conversation excerpt, extract only durable facts ABOUT THE USER that would still be true \
and useful weeks from now: who they are, people in their life, what they like and dislike, the \
games and projects they are into, habits, and plans that carry a date.
Do not extract: questions, anything Nox said, facts about Nox, moods of the moment, one-off \
requests, or anything listed under "Already known".
Write each fact as one short sentence in the language the user wrote in, in the third person, \
using the name {user}. At most {max_facts} facts.
Answer with a JSON array and nothing else: \
[{{"text": "...", "type": "user|preference|relationship|event|task", "confidence": 0.0-1.0}}]
Answer with [] when nothing qualifies.
"""


@dataclass(slots=True)
class _Exchange:
    user: str
    assistant: str


@dataclass
class FactCapture:
    """Learns facts about the user from finished turns, a little after the conversation pauses."""

    memory: MemoryService
    profile: UserProfile
    complete: Complete
    user_name: str
    embeddings: EmbeddingService | None = None
    settle_s: float = 45.0
    batch_turns: int = 4
    max_facts: int = 6
    clock: Callable[[], float] = field(default=time.monotonic)
    _pending: list[_Exchange] = field(default_factory=list, init=False, repr=False)
    _timer: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _running: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    #: What happened, for the health view and the tests.
    captured: int = field(default=0, init=False)
    skipped_duplicates: int = field(default=0, init=False)
    last_error: str = field(default="", init=False)

    def observe(self, user_text: str, assistant_text: str) -> None:
        """Called by the orchestrator after a model turn. Returns at once."""
        if not user_text.strip():
            return
        self._pending.append(_Exchange(user_text.strip(), assistant_text.strip()))
        self._schedule(0.0 if len(self._pending) >= self.batch_turns else self.settle_s)

    async def stop(self) -> None:
        for task in (self._timer, self._running):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._timer = self._running = None

    async def flush(self) -> int:
        """Extract from everything pending now; returns how many facts were stored."""
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)
            self._timer = None
        if self._running is not None and not self._running.done():
            await self._running
        exchanges, self._pending = self._pending, []
        if not exchanges:
            return 0
        return await self._capture(exchanges)

    # ---- internals ---------------------------------------------------------------------------

    def _schedule(self, delay_s: float) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()
        self._timer = asyncio.create_task(self._after(delay_s), name="nox-memory-capture")

    async def _after(self, delay_s: float) -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        if self._running is not None and not self._running.done():
            await self._running
        exchanges, self._pending = self._pending, []
        if not exchanges:
            return
        self._running = asyncio.create_task(self._capture_logged(exchanges))

    async def _capture_logged(self, exchanges: list[_Exchange]) -> None:
        try:
            await self._capture(exchanges)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - capture is background work; it never raises out
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("memory.capture_failed", error=self.last_error)

    async def _capture(self, exchanges: list[_Exchange]) -> int:
        facts = await self._extract(exchanges)
        stored = 0
        for fact in facts:
            if await self._is_known(fact.text):
                self.skipped_duplicates += 1
                continue
            try:
                await self.memory.create(fact.text, type=fact.type, source=_SOURCE, explicit=False)
            except MemoryWriteRefusedError as exc:
                log.info("memory.capture_refused", reason=str(exc))
                return stored
            stored += 1
        self.captured += stored
        if facts:
            log.info("memory.captured", facts=len(facts), stored=stored)
        return stored

    async def _extract(self, exchanges: list[_Exchange]) -> list[CapturedFact]:
        known = self.profile.block()
        excerpt = "\n".join(f"{self.user_name}: {e.user}\nNox: {e.assistant}" for e in exchanges)
        prompt = _EXTRACTION_SYSTEM.format(user=self.user_name, max_facts=self.max_facts)
        if known:
            prompt += f"\nAlready known:\n{known}\n"
        request = AiRequest(
            request_id=f"capture-{uuid.uuid4().hex[:8]}",
            role=AiRole.BACKGROUND,
            messages=[
                Message(role="system", content=prompt),
                Message(role="user", content=f"Conversation excerpt:\n{excerpt}"),
            ],
            max_tokens=400,
            temperature=0.1,
            timeout_s=60.0,
            stream=False,
        )
        response = await self.complete(request)
        facts = extract_facts_json(response.text)
        return [f for f in facts if f.confidence >= 0.5][: self.max_facts]

    async def _is_known(self, text: str) -> bool:
        wanted = normalise_fact(text)
        if any(normalise_fact(row.text) == wanted for row in self.profile.items()):
            return True
        if self.embeddings is None:
            return False
        try:
            hits = await self.embeddings.search(text, k=3, kind="memory_item")
        except Exception as exc:  # noqa: BLE001 - a search that fails is not a duplicate
            log.debug("memory.capture_dedupe_failed", error=str(exc))
            return False
        return any(hit.score >= DUPLICATE_SCORE for hit in hits)
