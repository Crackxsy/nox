"""The output device is one stream across the sentences of an answer, not one per sentence.

`SoundDeviceOutput` runs against a fake `sounddevice` whose "device" is pulled by the test: it asks
the output's callback for blocks the way PortAudio does, and keeps what it received. Waiting in a
thread is replaced by pulling blocks until the output says it is drained, so nothing here uses a
thread, a timer or real audio.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import numpy as np
import pytest

from nox.voice import audio
from nox.voice.audio import SoundDeviceOutput
from nox.voice.base import Channel

RATE = 22050
BLOCK = 256  # frames per device block


class FakeStream:
    def __init__(self, device: Any, samplerate: int, callback: Callable[..., None], finished: Any):
        self.device = device
        self.samplerate = samplerate
        self.callback = callback
        self.finished = finished
        self.started = False
        self.stopped = 0
        self.aborted = False
        self.closed = False
        self.received: list[int] = []

    def start(self) -> None:
        self.started = True

    def stop(self, ignore_errors: bool = True) -> None:
        self.stopped += 1

    def abort(self, ignore_errors: bool = True) -> None:
        self.aborted = True

    def close(self, ignore_errors: bool = True) -> None:
        self.closed = True

    def pull(self, status: Any = None) -> np.ndarray:
        """One block, as the device would ask for it."""
        block = np.zeros((BLOCK, 1), dtype=np.int16)
        self.callback(block, BLOCK, None, status)
        self.received.extend(block[:, 0].tolist())
        return block[:, 0]


class FakeSd:
    def __init__(self) -> None:
        self.streams: list[FakeStream] = []

    def OutputStream(self, **kwargs: Any) -> FakeStream:  # noqa: N802 - the library's name
        stream = FakeStream(
            kwargs["device"], kwargs["samplerate"], kwargs["callback"], kwargs["finished_callback"]
        )
        self.streams.append(stream)
        return stream

    @property
    def open_streams(self) -> list[FakeStream]:
        return [s for s in self.streams if not s.closed]

    def pull_all(self) -> None:
        for stream in self.open_streams:
            if not stream.aborted:
                stream.pull()


@pytest.fixture
def sd(monkeypatch: pytest.MonkeyPatch) -> FakeSd:
    fake = FakeSd()
    monkeypatch.setattr(audio, "_sd", lambda: fake)

    async def to_thread(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run on the loop; a wait for the device to drain pulls blocks until it has."""
        if getattr(function, "__name__", "") == "wait_drained":
            for _ in range(10_000):
                if function(0):
                    return True
                fake.pull_all()
                await asyncio.sleep(0)
            return False
        return function(*args, **kwargs)

    monkeypatch.setattr(audio.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(audio, "resolve_device", lambda name, *, kind: {"": None}.get(name, 7))
    return fake


def pcm(samples: int, value: int = 1000) -> bytes:
    return np.full(samples, value, dtype=np.int16).tobytes()


async def chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def say(out: SoundDeviceOutput, *parts: bytes, rate: int = RATE, uid: str = "u") -> None:
    await out.play(chunks(*parts), rate, Channel.PRIVATE, utterance_id=uid)


def made(**kwargs: Any) -> SoundDeviceOutput:
    return SoundDeviceOutput(volume=1.0, **kwargs)


async def test_the_sentences_of_an_answer_share_one_open_stream(sd: FakeSd) -> None:
    out = made()

    await say(out, pcm(1000), uid="s1")
    await say(out, pcm(1000), uid="s2")
    await say(out, pcm(1000), uid="s3")

    assert len(sd.streams) == 1  # opened once
    assert sd.streams[0].started and not sd.streams[0].closed  # and kept open
    assert out.streams_open == 1


async def test_what_the_device_receives_is_the_sentences_with_silence_where_nothing_plays(
    sd: FakeSd,
) -> None:
    out = made()
    await say(out, pcm(BLOCK * 3, 500))
    sd.streams[0].pull()  # nothing is playing between the sentences: the stream goes on in silence
    sd.streams[0].pull()
    await say(out, pcm(BLOCK * 2, 900))

    received = np.array(sd.streams[0].received)
    assert set(received.tolist()) <= {0, 500, 900}
    assert (received == 500).sum() == BLOCK * 3 and (received == 900).sum() == BLOCK * 2
    first = int(np.argmax(received == 500))
    assert (received[first : first + BLOCK * 3] == 500).all()  # no hole inside a sentence


async def test_a_different_sample_rate_needs_a_new_stream_and_the_old_one_is_closed(
    sd: FakeSd,
) -> None:
    out = made()
    await say(out, pcm(500), rate=22050)

    await say(out, pcm(500), rate=24000)

    assert [s.samplerate for s in sd.streams] == [22050, 24000]
    assert sd.streams[0].closed and not sd.streams[1].closed


async def test_another_device_needs_a_new_stream(sd: FakeSd) -> None:
    out = made()
    await say(out, pcm(500))

    out.set_devices(private_device="Headphones", stream_device="")
    await say(out, pcm(500))

    assert [s.device for s in sd.streams] == [None, 7]
    assert sd.streams[0].closed and not sd.streams[1].closed


async def test_the_stream_is_closed_after_the_silence_has_lasted(sd: FakeSd) -> None:
    out = made(linger_s=0.0)

    await say(out, pcm(500))
    for _ in range(5):
        await asyncio.sleep(0)  # the linger timer runs out

    assert sd.streams[0].closed and sd.streams[0].stopped == 1  # played out, then released
    assert out.streams_open == 0


async def test_a_sentence_inside_the_linger_time_keeps_the_stream(sd: FakeSd) -> None:
    out = made(linger_s=3600.0)

    await say(out, pcm(500), uid="s1")
    for _ in range(5):
        await asyncio.sleep(0)  # the timer is waiting, nothing closes
    assert not sd.streams[0].closed
    await say(out, pcm(500), uid="s2")

    assert len(sd.streams) == 1
    await out.close()
    assert sd.streams[0].closed and out.streams_open == 0  # and close() ends it for good


async def test_a_stop_cuts_the_sentence_off_and_the_next_one_opens_a_fresh_stream(
    sd: FakeSd,
) -> None:
    out = made()
    started = asyncio.Event()

    async def endless() -> AsyncIterator[bytes]:
        for _ in range(1000):
            yield pcm(BLOCK)
            started.set()
            await asyncio.sleep(0)

    playing = asyncio.create_task(out.play(endless(), RATE, Channel.PRIVATE, utterance_id="s1"))
    await started.wait()

    await out.stop(reason="barge_in")
    await playing

    assert sd.streams[0].aborted  # dropped at once, not played out
    assert out.streams_open == 0  # nothing of a cut-off sentence is kept
    await say(out, pcm(500), uid="s2")
    assert len(sd.streams) == 2 and not sd.streams[1].closed


async def test_a_stop_while_nothing_plays_leaves_the_idle_stream_alone(sd: FakeSd) -> None:
    out = made()
    await say(out, pcm(500))

    await out.stop(reason="stop")

    assert not sd.streams[0].aborted and out.streams_open == 1
    await say(out, pcm(500), uid="s2")
    assert len(sd.streams) == 1


async def test_a_stop_that_arrives_before_the_sentence_starts_cancels_it(sd: FakeSd) -> None:
    out = made()
    gate = asyncio.Event()

    async def late() -> AsyncIterator[bytes]:
        await gate.wait()
        yield pcm(500)

    async with out._lock:  # noqa: SLF001 - another sentence is playing: this one waits its turn
        waiting = asyncio.create_task(out.play(late(), RATE, Channel.PRIVATE, utterance_id="s2"))
        await asyncio.sleep(0)
        await out.stop(reason="barge_in")
    gate.set()
    await waiting

    assert sd.streams == []  # it never reached a device


async def test_a_device_that_died_is_reopened_for_the_next_sentence(sd: FakeSd) -> None:
    out = made()
    await say(out, pcm(500), uid="s1")

    sd.streams[0].finished()  # PortAudio: the stream ended on its own (device unplugged)
    await say(out, pcm(500), uid="s2")

    assert len(sd.streams) == 2 and sd.streams[0].closed and not sd.streams[1].closed


async def test_a_stream_that_cannot_be_opened_is_not_kept(
    sd: FakeSd, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = made()

    def refuse(**kwargs: Any) -> FakeStream:
        raise OSError("device busy")

    monkeypatch.setattr(sd, "OutputStream", refuse)
    with pytest.raises(OSError, match="device busy"):
        await say(out, pcm(500))

    assert out.streams_open == 0


async def test_both_channels_share_streams_per_device(sd: FakeSd) -> None:
    out = made(stream_device="CABLE")

    await out.play(chunks(pcm(500)), RATE, Channel.BOTH, utterance_id="s1")
    await out.play(chunks(pcm(500)), RATE, Channel.BOTH, utterance_id="s2")

    assert [s.device for s in sd.streams] == [None, 7]  # two devices, each opened once
    assert out.streams_open == 2


async def test_the_callback_scales_by_the_volume_and_pads_a_short_block_with_silence(
    sd: FakeSd,
) -> None:
    out = SoundDeviceOutput(volume=0.5)
    await say(out, pcm(100, 1000))

    block = sd.streams[0].received[:BLOCK]

    assert block[:100] == [500] * 100 and block[100:] == [0] * (BLOCK - 100)


async def test_a_synthesis_that_falls_behind_the_device_counts_as_an_underrun(sd: FakeSd) -> None:
    out = made()
    fed = asyncio.Event()
    go = asyncio.Event()

    async def stalling() -> AsyncIterator[bytes]:
        yield pcm(BLOCK)
        fed.set()
        await go.wait()  # the engine has nothing for a while
        yield pcm(BLOCK)

    playing = asyncio.create_task(out.play(stalling(), RATE, Channel.PRIVATE, utterance_id="s1"))
    await fed.wait()
    stream = sd.streams[0]
    stream.pull()  # takes the first block
    stream.pull()  # nothing there, though more is coming: the device played silence
    go.set()
    await playing

    assert out._players[0].underruns >= 1  # noqa: SLF001
    # whereas the silence between sentences is not one:
    before = out._players[0].underruns  # noqa: SLF001
    stream.pull()
    stream.pull()
    assert out._players[0].underruns == before  # noqa: SLF001


async def test_the_level_follows_what_is_played_and_falls_to_zero_in_the_silence(
    sd: FakeSd,
) -> None:
    out = made()
    fed, go = asyncio.Event(), asyncio.Event()

    async def one_loud_block() -> AsyncIterator[bytes]:
        yield pcm(BLOCK, 16384)
        fed.set()
        await go.wait()

    playing = asyncio.create_task(
        out.play(one_loud_block(), RATE, Channel.PRIVATE, utterance_id="s1")
    )
    await fed.wait()
    stream = sd.streams[0]

    stream.pull()
    assert out.level == pytest.approx(0.5, abs=0.01)  # half of full scale, over the whole block

    stream.pull()
    assert out.level == 0.0  # the device asked for more and there was nothing

    go.set()
    await playing


async def test_mute_plays_nothing_and_opens_nothing(sd: FakeSd) -> None:
    out = made()

    await out.play(chunks(pcm(500)), RATE, Channel.MUTE, utterance_id="s1")

    assert sd.streams == []
