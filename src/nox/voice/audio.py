"""sounddevice implementations of `AudioOutput` and `AudioInput`.

Output: one PortAudio callback stream per target device - the private headphones and, when
configured, a virtual audio cable for the stream. `both` writes the same PCM to both devices and
`mute` consumes and discards it. The streams stay open for a few seconds after a sentence, so the
sentences of one answer are one continuous stream (silence fills the gaps) instead of a device that
is opened and closed per sentence. `stop` aborts the streams, which drops buffered audio
immediately, well inside the barge-in budget.

Input: 16 kHz mono float32 frames of 30 ms. The capture stream exists only while `enabled` is True,
so a closed gate means no open device and no microphone indicator, not merely discarded frames.
Device names are resolved once and cached, because PortAudio enumeration is slow enough to be felt
on the event loop. Raw audio is never written to disk here.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from nox.util.aio import aclose
from nox.voice._logging import get_logger
from nox.voice.base import Channel

log = get_logger(__name__)

_PREFERRED_HOSTAPIS = ("Windows WASAPI", "MME", "Windows DirectSound")


class AudioUnavailableError(RuntimeError):
    """No output device for the requested channel. Nox never pretends to have spoken."""


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


def output_device_names() -> list[str]:
    """The output devices a user can choose from, one entry per device.

    The same device appears once per host API, MME with its name cut to 31 characters; the WASAPI
    names are the full ones, so they are offered when there are any. Each name resolves back to
    that device through `resolve_device`.
    """
    devices = [d for d in list_devices() if d["outputs"] != "0"]
    wasapi = [d for d in devices if d["hostapi"] == "Windows WASAPI"]
    return sorted({d["name"] for d in (wasapi or devices)}, key=str.casefold)


def input_device_names() -> list[str]:
    """The input devices PortAudio currently lists, one entry per device and host API.

    Blocking: it walks the host APIs, so the capture supervisor calls it from a thread. It is
    compared with an earlier answer to notice a headset that was plugged in while the microphone
    was down. The list is PortAudio's own and is not refreshed until the library is initialised
    again, which is never done here because it would cut off playing speech; so a device plugged in
    later may only show up after the voice service restarts.
    """
    return sorted(f"{d['hostapi']}:{d['name']}" for d in list_devices() if d["inputs"] != "0")


def _device_index(value: str) -> int | None:
    """`"#12"` or `"12"` names a device by its PortAudio index; anything else is a name."""
    text = value.strip().removeprefix("#")
    return int(text) if text.isdigit() else None


def resolve_device(name_substring: str, *, kind: str) -> int | None:
    """Map a device setting to a device index; '' means the system default (None).

    The setting is a case-insensitive name substring, or an index written as `#12` (indices move
    when devices are plugged in, so the settings page stores names). Preference order among name
    matches: WASAPI, MME, DirectSound, then anything else. Raises AudioUnavailableError when a
    non-empty setting matches nothing, or an index names a device without that direction.
    """
    needle = name_substring.strip().lower()
    if not needle:
        return None
    key = "max_output_channels" if kind == "output" else "max_input_channels"
    sd = _sd()
    index = _device_index(needle)
    if index is not None:
        devices = sd.query_devices()
        if index >= len(devices) or devices[index][key] <= 0:
            raise AudioUnavailableError(f"no {kind} device with index {index}")
        return index
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


def block_level(pcm: np.ndarray, frames: int) -> float:
    """RMS of one int16 output block as 0..1 (full scale = 1), counting silence padding."""
    if frames <= 0 or pcm.size == 0:
        return 0.0
    x = pcm.astype(np.float32) / 32768.0
    return float(min(1.0, np.sqrt(float(np.dot(x, x)) / frames)))


class _DevicePlayer:
    """One callback-driven int16 mono output stream fed from a byte buffer.

    The stream stays open between utterances and plays silence while the buffer is empty, so a
    sentence that follows another one starts without opening a device (which costs tens of
    milliseconds and is heard as a pause). `idle` says that everything fed so far has been handed
    to the device; `close` is the only way the stream ends, apart from a device that fails.
    """

    def __init__(self, device: int | None, sample_rate: int, volume: float) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.volume = volume
        self._buf = bytearray()
        self._lock = threading.Lock()
        #: Set while the buffer is empty and the device has taken everything fed before.
        self._idle = threading.Event()
        self._idle.set()
        self._stream: Any = None
        #: An utterance is being fed: an empty buffer now means the synthesis fell behind.
        self._expecting = False
        #: The stream ended on its own (device gone) or was aborted: it cannot be reused.
        self.dead = False
        self.underruns = 0
        #: RMS loudness (0..1) of the block the device took last. Written by the audio thread,
        #: read by the pipeline's level pacer; a float assignment is atomic, no lock needed.
        self.level = 0.0

    def open(self) -> None:
        sd = _sd()
        self._stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            device=self.device,
            callback=self._callback,
            latency="low",
            finished_callback=self._finished,
        )
        self._stream.start()

    def _finished(self) -> None:
        """The stream stopped: whoever waits for it to drain must not wait for ever."""
        self.dead = True
        self._idle.set()

    def _callback(self, outdata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
        if status and status.output_underflow:
            self.underruns += 1
        need = frames * 2
        with self._lock:
            chunk = bytes(self._buf[:need])
            del self._buf[:need]
            if not chunk:
                self._idle.set()
                if self._expecting:
                    self.underruns += 1
        if chunk:
            pcm = np.frombuffer(chunk, dtype=np.int16)
            if self.volume != 1.0:
                pcm = (pcm.astype(np.float32) * self.volume).clip(-32768, 32767).astype(np.int16)
            outdata[: pcm.size, 0] = pcm
            outdata[pcm.size :, 0] = 0
            self.level = block_level(pcm, frames)
        else:
            outdata.fill(0)
            self.level = 0.0

    def begin_utterance(self) -> None:
        with self._lock:
            self._expecting = True

    def end_utterance(self) -> None:
        with self._lock:
            self._expecting = False

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm)
            self._idle.clear()

    def wait_drained(self, timeout: float) -> bool:
        """Block until the device has taken everything fed so far (or `timeout` ran out)."""
        return self._idle.wait(timeout)

    def abort(self) -> None:
        """Drop what is buffered and stop the stream at once; the player cannot be reused."""
        with self._lock:
            self._buf.clear()
            self._expecting = False
        self.dead = True
        if self._stream is not None:
            try:
                self._stream.abort(ignore_errors=True)
            finally:
                self._idle.set()
        else:
            self._idle.set()

    def close(self) -> None:
        """Let the device play out what it holds, then release it."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            if not self.dead:
                stream.stop(ignore_errors=True)
        finally:
            stream.close(ignore_errors=True)
            self.dead = True


class SoundDeviceOutput:
    """AudioOutput backed by sounddevice; devices are chosen by name substring at construction.

    One output stream per target device stays open between sentences (`linger_s`), so the sentences
    of one answer are one continuous stream with silence where nothing is playing, not a series
    of streams that are opened and closed. The streams are closed after `linger_s` of silence,
    when the target devices or the sample rate change, and on `stop` and `close`.
    """

    def __init__(
        self,
        *,
        private_device: str = "",
        stream_device: str = "",
        volume: float = 0.8,
        drain_timeout_s: float = 30.0,
        linger_s: float = 3.0,
    ) -> None:
        self._private_name = private_device
        self._stream_name = stream_device
        self.volume = volume
        self._drain_timeout_s = drain_timeout_s
        self._linger_s = linger_s
        self._players: list[_DevicePlayer] = []
        #: The (targets, sample rate) the open players were opened for.
        self._session_key: tuple[tuple[int | None, ...], int] | None = None
        self._linger_task: asyncio.Task[None] | None = None
        #: An utterance is being fed right now (as opposed to an idle open stream).
        self._busy = False
        #: Incremented by every `stop()`. An utterance that started under an older generation is
        #: cancelled, so a stop arriving while it was still queued is not lost.
        self._stop_generation = 0
        self._resolved: dict[str, int | None] = {}
        self._lock = asyncio.Lock()
        self.last_stop_reason: str = ""
        self.last_stop_latency_ms: float = 0.0

    def devices(self) -> list[dict[str, str]]:
        return list_devices()

    def forget_devices(self) -> None:
        """Drop the resolved-device cache, e.g. after the user changed the audio configuration."""
        self._resolved.clear()

    def set_devices(self, *, private_device: str, stream_device: str) -> None:
        """Speak on other devices from the next utterance on (Settings -> Voice, live).

        `private_device` is where Nox speaks to the user (empty: the system default);
        `stream_device` is the virtual cable OBS records Nox from (empty: none). An utterance
        already playing finishes where it started.
        """
        self._private_name = private_device
        self._stream_name = stream_device
        self._resolved.clear()
        log.info(
            "audio.devices_changed",
            private=private_device or "default",
            stream=stream_device or "none",
        )

    @property
    def level(self) -> float:
        """Loudness (0..1) of what is playing right now; 0 when nothing is."""
        return max((p.level for p in self._players), default=0.0)

    @property
    def device_names(self) -> tuple[str, str]:
        """The configured (private, stream) device settings."""
        return self._private_name, self._stream_name

    @property
    def streams_open(self) -> int:
        """How many output streams are open right now, playing or waiting for the next sentence."""
        return len(self._players)

    async def _resolve(self, name: str) -> int | None:
        """Device index for a configured name, resolved once and then cached.

        `sd.query_devices` walks every host API and takes tens of milliseconds; doing that per
        utterance, on the event loop, was a measurable stutter before every reply.
        """
        if name in self._resolved:
            return self._resolved[name]
        index = await asyncio.to_thread(resolve_device, name, kind="output")
        self._resolved[name] = index
        return index

    async def _targets(self, channel: Channel) -> list[int | None]:
        if channel == Channel.MUTE:
            return []
        private = await self._resolve(self._private_name)
        if channel == Channel.PRIVATE:
            return [private]
        if not self._stream_name:
            if channel == Channel.STREAM:
                raise AudioUnavailableError(
                    "stream channel requested but no stream device configured"
                )
            return [private]
        stream = await self._resolve(self._stream_name)
        if channel == Channel.STREAM:
            return [stream]
        return [private, stream] if stream != private else [private]

    async def _release_players(self) -> None:
        """Close the open streams (letting them play out what they hold). Call under the lock."""
        players, self._players = self._players, []
        self._session_key = None
        for p in players:
            await asyncio.to_thread(p.close)

    async def _players_for(
        self, targets: list[int | None], sample_rate: int
    ) -> list[_DevicePlayer]:
        """The open streams for these targets: the ones left from the sentence before when they
        are the same and still alive, otherwise new ones. Call under the lock."""
        key = (tuple(targets), sample_rate)
        if self._players and self._session_key == key and not any(p.dead for p in self._players):
            return self._players
        await self._release_players()
        players = [_DevicePlayer(dev, sample_rate, self.volume) for dev in targets]
        try:
            for p in players:
                await asyncio.to_thread(p.open)
        except BaseException:
            for p in players:
                await asyncio.to_thread(p.close)
            raise
        self._players = players
        self._session_key = key
        return players

    def _cancel_linger(self) -> None:
        task, self._linger_task = self._linger_task, None
        if task is not None and not task.done():
            task.cancel()

    def _arm_linger(self) -> None:
        """Close the streams after `linger_s` of silence, unless another sentence comes first."""
        self._cancel_linger()
        if self._players:
            self._linger_task = asyncio.create_task(self._linger_close(), name="audio-linger")

    async def _linger_close(self) -> None:
        await asyncio.sleep(self._linger_s)
        async with self._lock:
            if not self._busy:
                await self._release_players()

    async def close(self) -> None:
        """Close the output streams (shutdown, or a device change that must take effect now)."""
        self._cancel_linger()
        async with self._lock:
            await self._release_players()

    async def play(
        self,
        pcm_chunks: AsyncIterator[bytes],
        sample_rate: int,
        channel: Channel,
        *,
        utterance_id: str,
    ) -> None:
        # Captured before the lock: a `stop()` that lands while this utterance waits its turn
        # must cancel it, instead of being cleared away when it finally starts.
        generation = self._stop_generation
        async with self._lock:
            self._cancel_linger()

            def cancelled() -> bool:
                return self._stop_generation != generation

            if cancelled():
                await aclose(pcm_chunks)
                return
            targets = await self._targets(channel)
            if channel == Channel.MUTE:
                async for _ in pcm_chunks:  # consume so synthesis threads finish cleanly
                    if cancelled():
                        break
                return
            players: list[_DevicePlayer] = []
            finished = False
            self._busy = True
            try:
                players = await self._players_for(targets, sample_rate)
                for p in players:
                    p.begin_utterance()
                async for chunk in pcm_chunks:
                    if cancelled():
                        break
                    for p in players:
                        p.feed(chunk)
                for p in players:
                    p.end_utterance()
                if not cancelled():
                    for p in players:
                        drained = await asyncio.to_thread(p.wait_drained, self._drain_timeout_s)
                        if not drained:
                            log.warning("audio.drain_timeout", utterance_id=utterance_id)
                            p.abort()
                finished = not cancelled() and not any(p.dead for p in players)
                log.debug(
                    "audio.play_done",
                    utterance_id=utterance_id,
                    channel=str(channel),
                    underruns=sum(p.underruns for p in players),
                    stopped=cancelled(),
                )
            finally:
                self._busy = False
                if finished:
                    self._arm_linger()
                else:
                    # Cut off, failed or a dead device: nothing worth keeping open.
                    await self._release_players()

    async def stop(self, *, reason: str) -> None:
        t0 = time.perf_counter()
        self._stop_generation += 1
        self.last_stop_reason = reason
        # Only an utterance that is being fed is cut off; an idle stream that is kept open for the
        # next sentence has nothing to abort and stays usable.
        players = list(self._players) if self._busy else []
        for p in players:
            await asyncio.to_thread(p.abort)
        self.last_stop_latency_ms = (time.perf_counter() - t0) * 1000.0
        if players:
            log.info("audio.stopped", reason=reason, latency_ms=round(self.last_stop_latency_ms, 1))


# ---- Input -------------------------------------------------------------------------------------


class SoundDeviceInput:
    """AudioInput: 16 kHz mono float32 frames of `frame_ms`.

    The PortAudio capture stream is opened by `set_enabled(True)` and closed again by
    `set_enabled(False)`, so while capture is not permitted no stream exists at all - which is
    what `listening_mode: ptt_only` and every privacy mode rely on.
    """

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

    async def set_enabled(self, value: bool) -> None:
        """Open or close the capture stream. Opening a PortAudio device blocks, hence async."""
        if value:
            if not self._running:
                raise RuntimeError("SoundDeviceInput.start() not called")
            if self._stream is None:
                await asyncio.to_thread(self._open_stream)
            self._enabled.set()
            return
        self._enabled.clear()
        self._pending = np.zeros(0, dtype=np.float32)
        stream, self._stream = self._stream, None
        if stream is not None:
            await asyncio.to_thread(self._close_stream, stream)
            log.info("audio.input_closed")

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
        """Prepare the frame queue. No device is opened until `set_enabled(True)`."""
        if self._running:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
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
