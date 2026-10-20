"""Sentence-wise streaming for TTS engines whose renderer is a blocking call.

The renderer runs in a worker thread, one sentence at a time, and every rendered sentence is handed
to the async consumer as soon as it exists, so the first sentence is heard while later ones are
still being computed. Closing the consumer (barge-in, `tts.stop`) stops the worker after the
sentence it is rendering - it never starts another one.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator, Callable, Iterable

_END = object()


async def stream_in_thread(
    render: Callable[[str], bytes],
    sentences: Iterable[str],
    *,
    thread_name: str,
) -> AsyncGenerator[bytes]:
    """Yield `render(sentence)` for each sentence, computed off the event loop, in order."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | BaseException | object] = asyncio.Queue()
    cancel = threading.Event()
    pending = list(sentences)

    def put(item: bytes | BaseException | object) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:  # the loop closed while a sentence was rendering - nobody listens
            cancel.set()

    def worker() -> None:
        try:
            for sentence in pending:
                if cancel.is_set():
                    break
                put(render(sentence))
        except Exception as exc:  # noqa: BLE001 - re-raised in the consumer below
            put(exc)
        finally:
            put(_END)

    threading.Thread(target=worker, name=thread_name, daemon=True).start()
    try:
        while True:
            item = await queue.get()
            if item is _END:
                break
            if isinstance(item, BaseException):
                raise item
            assert isinstance(item, bytes)
            yield item
    finally:
        cancel.set()
