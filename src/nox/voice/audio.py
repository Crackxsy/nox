"""sounddevice implementations of AudioOutput and AudioInput (voice/base.py, ADR-009, FR-5.5/5.6).

Output: one PortAudio callback stream per target device (private headphones, stream = VB-Audio
Cable);
`both` writes the same PCM to both, `mute` consumes and discards. `stop()` aborts the streams, which
drops buffered audio immediately (well under the 200 ms barge-in budget). Input: 16 kHz mono float32
frames of 30 ms that flow into an asyncio queue only while `enabled` is True (privacy gate). Raw
audio
is never written to disk here.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from nox.voice._logging import get_logger
from nox.voice.base import Channel

log = get_logger(__name__)

_PREFERRED_HOSTAPIS = ("Windows WASAPI", "MME", "Windows DirectSound")


class AudioUnavailableError(RuntimeError):
    """No output device for the requested channel (FR-5.10: never 'speak' silently)."""


def _sd() -> Any:
    import sounddevice

    return sounddevice


def list_devices() -> list[dict[str, str]]:
    """Enumerate audio devices (all host APIs) as plain string dicts for IPC/selftest output."""
    sd = _sd()
    hostapis = sd.query_hostapis()
    default_in, default_out = sd.default.device
    result: list[dict[str, str]] = []
    for index, dev in enumerate(sd.query_devices()):
        api = hostapis[dev["hostapi"]]["name"]
        result.append(
            {
                "index": str(index),
                "name": str(dev["name"]),
                "hostapi": str(api),
                "inputs": str(dev["max_input_channels"]),
                "outputs": str(dev["max_output_channels"]),
                "default_samplerate": str(int(dev["default_samplerate"])),
                "default_input": str(index == default_in),
                "default_output": str(index == default_out),
            }
        )
    return result


def resolve_device(name_substring: str, *, kind: str) -> int | None:
    """Map a case-insensitive name substring to a device index; '' means the system default (None).

    Preference order among matches: WASAPI, MME, DirectSound, then anything else. Raises
    AudioUnavailableError when a non-empty name matches nothing.
    """
    needle = name_substring.strip().lower()
    if not needle:
        return None
    key = "max_output_channels" if kind == "output" else "max_input_channels"
    sd = _sd()
    hostapis = sd.query_hostapis()
    candidates: list[tuple[int, int]] = []
    for index, dev in enumerate(sd.query_devices()):
        if dev[key] <= 0 or needle not in str(dev["name"]).lower():
            continue
        api = hostapis[dev["hostapi"]]["name"]
        rank = _PREFERRED_HOSTAPIS.index(api) if api in _PREFERRED_HOSTAPIS else 99
        candidates.append((rank, index))
    if not candidates:
        raise AudioUnavailableError(f"no {kind} device matches {name_substring!r}")
    candidates.sort()
    return candidates[0][1]


def resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Cheap linear-interpolation resampler for mono float32 (fine for speech at these rates)."""
    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float32, copy=False)
    n_out = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.asarray(np.interp(x_new, x_old, audio), dtype=np.float32)


# ---- Output ------------------------------------------------------------------------------------


class _DevicePlayer:
    """One callback-driven int16 mono output stream fed from a byte buffer."""

    def __init__(self, device: int | None, sample_rate: int, volume: float) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.volume = volume
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._eof = False
        self._drained = threading.Event()
        self._stream: Any = None
        self.underruns = 0

    def open(self) -> None:
        sd = _sd()
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            device=self.device,
            callback=self._callback,
            latency="low",
            finished_callback=self._drained.set,
        )
        self._stream.start()

    def _callback(self, outdata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
        sd = _sd()
        if status and status.output_underflow:
            self.underruns += 1
        need = frames * 2
        with self._lock:
            chunk = bytes(self._buf[:need])
            del self._buf[:need]
            eof = self._eof and not self._buf
        if chunk:
            pcm = np.frombuffer(chunk, dtype=np.int16)
            if self.volume != 1.0:
                pcm = (pcm.astype(np.float32) * self.volume).clip(-32768, 32767).astype(np.int16)
            outdata[: pcm.size, 0] = pcm
            outdata[pcm.size :, 0] = 0
        else:
            outdata.fill(0)
        if eof:
            raise sd.CallbackStop

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm)

    def end_of_input(self) -> None:
        with self._lock:
            self._eof = True
            if not self._buf:
                pass  # the callback raises CallbackStop on its next invocation

    def wait_drained(self, timeout: float) -> bool:
        return self._drained.wait(timeout)

    def abort(self) -> None:
        with self._lock:
            self._buf.clear()
            self._eof = True
        if self._stream is not None:
            try:
                self._stream.abort(ignore_errors=True)
            finally:
                self._drained.set()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.close(ignore_errors=True)
            finally:
                self._stream = None


class SoundDeviceOutput:
    """AudioOutput backed by sounddevice; devices are chosen by name substring at construction."""

    def __init__(
        self,
        *,
        private_device: str = "",
        stream_device: str = "",
        volume: float = 0.8,
        drain_timeout_s: float = 30.0,
    ) -> None:
        self._private_name = private_device
        self._stream_name = stream_device
        self.volume = volume
        self._drain_timeout_s = drain_timeout_s
        self._players: list[_DevicePlayer] = []
        self._stopped = threading.Event()
        self._lock = asyncio.Lock()
        self.last_stop_reason: str = ""
        self.last_stop_latency_ms: float = 0.0

    def devices(self) -> list[dict[str, str]]:
        return list_devices()

    def _targets(self, channel: Channel) -> list[int | None]:
        if channel == Channel.MUTE:
            return []
        private = resolve_device(self._private_name, kind="output")
        if channel == Channel.PRIVATE:
            return [private]
        if not self._stream_name:
            if channel == Channel.STREAM:
                raise AudioUnavailableError(
                    "stream channel requested but no stream device configured"
                )
            return [private]
        stream = resolve_device(self._stream_name, kind="output")
        if channel == Channel.STREAM:
            return [stream]
        return [private, stream] if stream != private else [private]

    async def play(
        self,
        pcm_chunks: AsyncIterator[bytes],
        sample_rate: int,
        channel: Channel,
        *,
        utterance_id: str,
    ) -> None:
        async with self._lock:
            self._stopped.clear()
            self.last_stop_reason = ""
            targets = self._targets(channel)
            if channel == Channel.MUTE:
                async for _ in pcm_chunks:  # consume so synthesis threads finish cleanly
                    if self._stopped.is_set():
                        break
                return
            players = [_DevicePlayer(dev, sample_rate, self.volume) for dev in targets]
            self._players = players
            try:
                for p in players:
                    await asyncio.to_thread(p.open)
                async for chunk in pcm_chunks:
                    if self._stopped.is_set():
                        break
                    for p in players:
                        p.feed(chunk)
                if not self._stopped.is_set():
                    for p in players:
                        p.end_of_input()
                    for p in players:
                        drained = await asyncio.to_thread(p.wait_drained, self._drain_timeout_s)
                        if not drained:
                            log.warning("audio.drain_timeout", utterance_id=utterance_id)
                            p.abort()
                log.debug(
                    "audio.play_done",
                    utterance_id=utterance_id,
                    channel=str(channel),
                    underruns=sum(p.underruns for p in players),
                    stopped=self._stopped.is_set(),
                )
            finally:
                for p in players:
                    await asyncio.to_thread(p.close)
                self._players = []

    async def stop(self, *, reason: str) -> None:
        t0 = time.perf_counter()
        self._stopped.set()
        self.last_stop_reason = reason
        players = list(self._players)
        for p in players:
            await asyncio.to_thread(p.abort)
        self.last_stop_latency_ms = (time.perf_counter() - t0) * 1000.0
        if players:
            log.info("audio.stopped", reason=reason, latency_ms=round(self.last_stop_latency_ms, 1))


# ---- Input -------------------------------------------------------------------------------------


class SoundDeviceInput:
    """AudioInput: 16 kHz mono float32 frames of `frame_ms`; frames flow only while `enabled`."""

    def __init__(
        self,
        *,
        device: str = "",
        sample_rate: int = 16000,
        frame_ms: int = 30,
        queue_frames: int = 200,
    ) -> None:
        self._device_name = device
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._frame_len = sample_rate * frame_ms // 1000
        self._queue_max = queue_frames
        self._queue: asyncio.Queue[np.ndarray | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: Any = None
        self._enabled = threading.Event()
        self._running = False
        self._device_rate = sample_rate
        self._pending = np.zeros(0, dtype=np.float32)
        self.dropped_frames = 0

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def frame_ms(self) -> int:
        return self._frame_ms

    @property
    def enabled(self) -> bool:
        return self._enabled.is_set()

    @enabled.setter
    def enabled(self, value: bool) -> None:
        if value:
            self._enabled.set()
        else:
            self._enabled.clear()
            self._pending = np.zeros(0, dtype=np.float32)

    @property
    def running(self) -> bool:
        return self._running

    def _open_stream(self) -> None:
        sd = _sd()
        device = resolve_device(self._device_name, kind="input")
        rates = [self._sample_rate, 48000, 44100]
        last_error: Exception | None = None
        for rate in rates:
            try:
                blocksize = rate * self._frame_ms // 1000
                stream = sd.InputStream(
                    samplerate=rate,
                    channels=1,
                    dtype="float32",
                    device=device,
                    blocksize=blocksize,
                    callback=self._callback,
                    latency="low",
                )
                stream.start()
            except Exception as exc:  # noqa: BLE001 - PortAudio raises plain exceptions
                last_error = exc
                continue
            self._stream = stream
            self._device_rate = rate
            log.info("audio.input_opened", device=device, rate=rate)
            return
        raise AudioUnavailableError(f"cannot open input device {self._device_name!r}: {last_error}")

    def _callback(self, indata: np.ndarray, _frames: int, _time: Any, status: Any) -> None:
        if status and status.input_overflow:
            self.dropped_frames += 1
        if not self._enabled.is_set() or self._loop is None or self._queue is None:
            return
        mono = indata[:, 0].astype(np.float32, copy=True)
        if self._device_rate != self._sample_rate:
            mono = resample_linear(mono, self._device_rate, self._sample_rate)
        data = np.concatenate([self._pending, mono]) if self._pending.size else mono
        n = self._frame_len
        full = data.size // n
        for i in range(full):
            frame = data[i * n : (i + 1) * n]
            self._loop.call_soon_threadsafe(self._enqueue, frame)
        self._pending = data[full * n :]

    def _enqueue(self, frame: np.ndarray) -> None:
        if self._queue is None:
            return
        if self._queue.qsize() >= self._queue_max:
            try:
                self._queue.get_nowait()
                self.dropped_frames += 1
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(frame)

    async def start(self) -> None:
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        await asyncio.to_thread(self._open_stream)
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._enabled.clear()
        stream, self._stream = self._stream, None
        if stream is not None:
            await asyncio.to_thread(self._close_stream, stream)
        if self._queue is not None:
            self._queue.put_nowait(None)

    @staticmethod
    def _close_stream(stream: Any) -> None:
        stream.abort(ignore_errors=True)
        stream.close(ignore_errors=True)

    async def frames(self) -> AsyncIterator[np.ndarray]:
        if self._queue is None:
            raise RuntimeError("SoundDeviceInput.start() not called")
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            yield frame
