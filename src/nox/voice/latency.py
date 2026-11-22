"""How long a spoken turn takes, from the end of the user's utterance to the first sound back.

`TurnTimings` in the orchestrator measures what the core does (context, first token, first
sentence). What the user feels is more: the end-of-speech decision, transcription, the model, and
the speech synthesis in front of the first audio chunk. This tracker follows one voice turn through
the worker's pipeline and records those stages, so the felt latency is a number rather than an
impression:

    utterance ended -> transcribed -> transcript published -> first sentence handed to speech
    -> first audio chunk handed to the output device

The model's own first-token time is not visible from the worker; it is part of `reply_ms` (the
orchestrator's `TurnTimings` break it down). Only voice turns are measured: a turn exists from the
moment an addressed transcript is published, and ends at the first sound that follows. Everything is
local: the numbers go to the structured log (no content, only milliseconds) and to
`summary()`, which the `voice.latency` request returns. Time is injected, so tests use a fake clock.

A second number belongs to the same request: the *sentence gap*, the silence between the end of one
sentence and the start of the next one when the next one was already waiting. It is what the user
hears as a stutter in a long answer, and it has a budget (`SENTENCE_GAP_BUDGET_MS`) that the tests
hold the pipeline to.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

#: How many finished turns the percentiles are computed over.
LATENCY_WINDOW = 50
#: A published transcript whose answer starts later than this is not counted as one turn.
REPLY_TIMEOUT_S = 60.0
#: How many sentence gaps the percentiles are computed over: a long answer has dozens of them.
GAP_WINDOW = 100
#: The 95th-percentile silence between two sentences of one answer that is still heard as one
#: breath. The sentence-gap budget (see `LatencySummary.sentence_gap_ok`).
SENTENCE_GAP_BUDGET_MS = 120.0
#: Fewer gaps than this say nothing about the machine, so health does not judge on them.
GAP_MIN_SAMPLES = 8


@dataclass(frozen=True, slots=True)
class TurnLatency:
    """The stages of one turn in milliseconds, measured on one monotonic clock."""

    stt_ms: float
    #: Transcription finished until the transcript left the worker (gate bookkeeping, IPC send).
    publish_ms: float
    #: Transcript published until the first sentence was handed to speech: the core's work,
    #: dominated by the model.
    reply_ms: float
    #: First sentence handed to speech until its first audio chunk reached the output device.
    synth_ms: float
    #: Utterance ended until the first audio chunk reached the output device.
    total_ms: float
    #: Silence the end-of-speech detector waited for before it declared the utterance over (zero
    #: for push-to-talk). Time since the user's last word is `total_ms + endpoint_ms`.
    endpoint_ms: float


class Spread(BaseModel):
    p50: float
    p95: float


class LatencySummary(BaseModel):
    """What `voice.latency` returns: percentiles over the last `window` finished turns."""

    turns: int
    window: int
    #: Turns that began but never produced sound (interrupted, unanswered, replaced).
    abandoned: int
    total_ms: Spread | None
    stt_ms: Spread | None
    reply_ms: Spread | None
    synth_ms: Spread | None
    endpoint_ms: float
    last: dict[str, float] | None
    #: Silence between the end of a sentence and the start of the next one of the same answer,
    #: for sentences that were already waiting; None until there is one.
    sentence_gap_ms: Spread | None = None
    #: How many gaps the percentiles are over (up to `GAP_WINDOW`).
    sentence_gaps: int = 0
    #: The budget the 95th percentile is held to.
    sentence_gap_budget_ms: float = SENTENCE_GAP_BUDGET_MS
    #: Whether the 95th percentile is under the budget; None while there are no gaps yet.
    sentence_gap_ok: bool | None = None


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile: the smallest value at least `pct` percent of the data is below."""
    if not values:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _spread(values: list[float]) -> Spread | None:
    if not values:
        return None
    return Spread(p50=round(percentile(values, 50), 1), p95=round(percentile(values, 95), 1))


@dataclass(slots=True)
class TurnTicket:
    """One open turn; whoever holds it may report the turn's first sound."""

    ended_at: float
    transcribed_at: float
    published_at: float
    endpoint_ms: float
    said_at: float | None = None


class LatencyTracker:
    """Follows one voice turn at a time; a newer turn replaces an unfinished older one."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.perf_counter,
        window: int = LATENCY_WINDOW,
        reply_timeout_s: float = REPLY_TIMEOUT_S,
    ) -> None:
        self._clock = clock
        self._reply_timeout_s = reply_timeout_s
        self._turns: deque[TurnLatency] = deque(maxlen=window)
        self._gaps: deque[float] = deque(maxlen=GAP_WINDOW)
        self._open: TurnTicket | None = None
        self.abandoned = 0

    @property
    def window(self) -> int:
        return self._turns.maxlen or 0

    def begin(
        self, *, ended_at: float, transcribed_at: float, published_at: float, endpoint_ms: float
    ) -> None:
        """An addressed transcript has been published: the answer to it is the next sound."""
        self.abandon()
        self._open = TurnTicket(ended_at, transcribed_at, published_at, endpoint_ms)

    def say_requested(self) -> TurnTicket | None:
        """A sentence was handed to speech: the open turn's ticket if it is the first one."""
        turn = self._open
        if turn is None or turn.said_at is not None:
            return None
        now = self._clock()
        if now - turn.published_at > self._reply_timeout_s:
            self.abandon()
            return None
        turn.said_at = now
        return turn

    def first_audio(self, ticket: TurnTicket) -> TurnLatency | None:
        """The first audio chunk of the ticket's sentence reached the output.

        A ticket whose turn was replaced or abandoned meanwhile reports nothing.
        """
        turn = self._open
        if turn is None or turn is not ticket or turn.said_at is None:
            return None
        self._open = None
        now = self._clock()
        result = TurnLatency(
            stt_ms=_ms(turn.ended_at, turn.transcribed_at),
            publish_ms=_ms(turn.transcribed_at, turn.published_at),
            reply_ms=_ms(turn.published_at, turn.said_at),
            synth_ms=_ms(turn.said_at, now),
            total_ms=_ms(turn.ended_at, now),
            endpoint_ms=turn.endpoint_ms,
        )
        self._turns.append(result)
        return result

    def record_gap(self, started_at: float, ended_at: float) -> float:
        """One sentence gap: the previous sentence's audio ended at `started_at`, the next one's
        first chunk reached the output at `ended_at`. Returns the gap in milliseconds."""
        gap = _ms(started_at, ended_at)
        self._gaps.append(gap)
        return gap

    def gap_p95_ms(self) -> tuple[float, int] | None:
        """`(95th percentile in ms, number of gaps)`, or None before the first gap."""
        gaps = list(self._gaps)
        if not gaps:
            return None
        return round(percentile(gaps, 95), 1), len(gaps)

    def abandon(self) -> None:
        """The open turn will not produce sound (interrupted, replaced, stopped)."""
        if self._open is not None:
            self._open = None
            self.abandoned += 1

    def summary(self) -> LatencySummary:
        turns = list(self._turns)
        last = turns[-1] if turns else None
        gaps = list(self._gaps)
        gap_spread = _spread(gaps)
        return LatencySummary(
            sentence_gap_ms=gap_spread,
            sentence_gaps=len(gaps),
            sentence_gap_budget_ms=SENTENCE_GAP_BUDGET_MS,
            sentence_gap_ok=None if gap_spread is None else gap_spread.p95 < SENTENCE_GAP_BUDGET_MS,
            turns=len(turns),
            window=self.window,
            abandoned=self.abandoned,
            total_ms=_spread([t.total_ms for t in turns]),
            stt_ms=_spread([t.stt_ms for t in turns]),
            reply_ms=_spread([t.reply_ms for t in turns]),
            synth_ms=_spread([t.synth_ms for t in turns]),
            endpoint_ms=last.endpoint_ms if last else 0.0,
            last=None
            if last is None
            else {
                "stt_ms": last.stt_ms,
                "publish_ms": last.publish_ms,
                "reply_ms": last.reply_ms,
                "synth_ms": last.synth_ms,
                "total_ms": last.total_ms,
                "endpoint_ms": last.endpoint_ms,
            },
        )


def _ms(start: float, end: float) -> float:
    return round(max(0.0, end - start) * 1000.0, 1)


async def noting_first_chunk(
    chunks: AsyncIterator[bytes], on_first: Callable[[], None]
) -> AsyncIterator[bytes]:
    """Pass audio chunks through and call `on_first` as the first one is handed on."""
    first = True
    async for chunk in chunks:
        if first:
            first = False
            on_first()
        yield chunk
