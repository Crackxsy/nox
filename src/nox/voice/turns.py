"""Which reply turns are waiting to be spoken, which one is speaking, and which were cancelled.

A reply reaches the voice worker sentence by sentence, each sentence its own `tts.speak`, queued
behind the one playing. Cancelling only the sentence that happens to be playing - what barge-in
used to do - let the rest of the reply play on. The ledger cancels by turn instead: the sentence
playing, the sentences queued behind it, and any sentence of that turn that arrives later (the
language model may still be streaming the answer).

Cancelled turns are remembered in a bounded list; a turn old enough to fall out of it has long
stopped producing sentences.
"""

from __future__ import annotations

from collections import Counter, OrderedDict

from nox.voice.base import TtsRequest

#: Cancelled turns remembered, so late sentences of a cancelled reply are still dropped.
CANCELLED_TURNS_KEPT = 64


def turn_of(request: TtsRequest) -> str:
    """The turn an utterance belongs to; without a `turn_id` it is a turn of its own."""
    return request.turn_id or request.utterance_id


class TurnLedger:
    def __init__(self, kept: int = CANCELLED_TURNS_KEPT) -> None:
        self._kept = kept
        self._queued: Counter[str] = Counter()
        self._cancelled: OrderedDict[str, str] = OrderedDict()
        #: The turn whose sentence is playing right now, if any.
        self.current: str | None = None

    def cancelled(self, turn: str) -> str | None:
        """Why `turn` was cancelled, or None while it may still be spoken."""
        return self._cancelled.get(turn)

    def enqueue(self, turn: str) -> None:
        self._queued[turn] += 1

    def dequeue(self, turn: str) -> None:
        self._queued[turn] -= 1
        if self._queued[turn] <= 0:
            del self._queued[turn]

    def cancel_active(self, reason: str) -> None:
        """Cancel the turn playing and every turn with a sentence still queued."""
        turns = set(self._queued)
        if self.current is not None:
            turns.add(self.current)
        for turn in turns:
            # The latest reason wins: a kill phrase after a barge-in is what the user should see.
            self._cancelled[turn] = reason
            self._cancelled.move_to_end(turn)
        while len(self._cancelled) > self._kept:
            self._cancelled.popitem(last=False)
