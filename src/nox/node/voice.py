"""The voice satellite (C9): Nox listens and speaks on this device; only text crosses the network.

With the group `voice` enabled on this device (`nox node allow voice`, off by default) - and
switched on for it on the core as well, where it starts switched off (`nox.node.policy`,
`CORE_OPT_IN`) - the node runs the core's own voice pipeline (`nox.voice.pipeline`) here:

* **Listening** - push-to-talk (the default: the microphone is open only while it is held) or the
  wake word (the microphone stays open behind the wake-word gate). Wake word, voice activity and
  speech recognition (faster-whisper; `tiny` on a Pi, `base` elsewhere unless set) run on this
  device. Only the recognised **text** goes to the core (`nox/voice/heard`); audio never leaves
  the device and is never written anywhere - it lives in memory for one utterance.
* **Who may open the microphone** - all of: the group is enabled here and not narrowed by the
  core, the node is not stopped, it is connected to a core that understands satellites, the
  core's privacy allows the microphone right now (`nox/voice/gate`: off until the core says
  so - panic, safe mode, the capture switch, a privacy zone and the kill switch close it), and
  the local mute switch is off (`nox node voice mute`, the tray). Only this device unmutes.
* **Speaking** - the core's answer comes back as text, sentence by sentence (`nox/voice/say`),
  accepted only for a question this device asked in the last minutes; this device synthesises it
  (Supertonic 3, voice F3, by default; Piper when Supertonic is missing - the core's AI-voice
  disclosure rule for streams applies to that voice like to the core's own). `voice.say` lets the
  model say something here on purpose (low risk, receipt, budget, audit). The loudness of what is
  played goes to the core as a number (`nox/voice/level`), for a pet on this device.
* **Seen and heard** - while the microphone is open the tray shows it ("Nox hört zu",
  `voice.json` in the node's folder) and, with `earcon` on, a short tone says so when push-to-talk
  starts and when a question was taken.
* **"Nox, Notaus"** never reaches the core as text: it stops this node at once (as
  `nox node stop`), which stops Nox everywhere; only this device resumes.

No transcript is stored on this device; the node's audit records that something was heard and
how long it was, never what. On the core the turn follows its privacy mode like a turn spoken
into the core's own microphone, except that facts learned from it wait for confirmation
(`turn_origin("node")`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import platform
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nox.core.events.names import E
from nox.core.logging import get_logger
from nox.node import protocol as p
from nox.node.tools import NodeRefusal, NodeTool

log = get_logger(__name__)

__all__ = [
    "MUTE_FILE",
    "PTT_FILE",
    "STATE_FILE",
    "SayArgs",
    "VoiceEngines",
    "VoiceSatellite",
    "VoiceSettings",
    "voice_config",
    "voice_tools",
]

#: Marker files in the node's folder: muted here (only this device removes it), push-to-talk held
#: (written by the tray's key or `nox node voice talk`), and what the tray shows.
MUTE_FILE = "voice-muted"
PTT_FILE = "voice-ptt"
STATE_FILE = "voice.json"
#: A push-to-talk marker older than this is a key that was never released: ignored and removed.
PTT_STALE_S = 30.0
#: How long an answer to one of this device's questions may keep coming.
TURN_TTL_S = 180.0
MAX_TURNS = 8
MAX_SENTENCES = 40
#: How long the microphone stays closed after this device stopped speaking (its echo).
ECHO_TAIL_S = 0.3
#: Levels sent to the core per second at most (the pet needs no more).
LEVEL_HZ = 15
POLL_S = 0.2


class VoiceSettings(BaseModel):
    """The `voice:` part of this node's `satellites.yaml`. How it listens and speaks - not
    whether: that is the group `voice` (`nox node allow voice`)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["push_to_talk", "wake_word"] = "push_to_talk"
    wake_word: str = Field(default="Nox", min_length=2, max_length=32)
    language: Literal["de", "en", "auto"] = "de"
    #: Empty: by device - `tiny` on a Raspberry Pi (arm), `base` elsewhere.
    stt_model: Literal["", "tiny", "base", "small"] = ""
    tts_engine: Literal["supertonic", "piper"] = "supertonic"
    tts_voice: str = Field(default="", max_length=64)
    volume: float = Field(default=0.8, ge=0.0, le=1.0)
    #: A short tone when push-to-talk starts and when a question was taken.
    earcon: bool = True
    #: Where the model files are; empty: `models` in the node's folder.
    models_dir: str = Field(default="", max_length=400)


def small_device(machine: str | None = None) -> bool:
    arch = (machine if machine is not None else platform.machine()).lower()
    return arch.startswith(("arm", "aarch"))


def voice_config(settings: VoiceSettings, home: Path) -> Any:
    """The core's `VoiceConfig` for this device: its engines, sized for it."""
    from nox.core.config.assistant import VoiceConfig  # noqa: PLC0415

    model = settings.stt_model or ("tiny" if small_device() else "base")
    return VoiceConfig.model_validate(
        {
            "stt": {
                "model": model,
                "language": settings.language,
                "wake_word": settings.wake_word,
                "listening_mode": "ptt_only" if settings.mode == "push_to_talk" else "continuous",
                # No follow-up without the wake word on a satellite (one utterance per wake).
                "conversation_window_s": 0.0,
            },
            "tts": {
                "engine": settings.tts_engine,
                "voice": settings.tts_voice,
                "volume": settings.volume,
            },
            "models_dir": settings.models_dir or str(home / "models"),
        }
    )


@dataclass
class VoiceEngines:
    audio_in: Any
    audio_out: Any
    stt: Any
    tts: Any
    wake_gate: Any = None

    async def unload(self) -> None:
        for engine in (self.stt, self.tts):
            with contextlib.suppress(Exception):
                await engine.unload()


async def load_engines(settings: VoiceSettings, home: Path) -> VoiceEngines:
    """The core's engines on this device (`nox.worker.main`), loaded off the event loop."""
    from nox.worker.main import build_components, build_wake_gate, load_tts  # noqa: PLC0415

    cfg = voice_config(settings, home)
    parts = await asyncio.to_thread(build_components, cfg, service="voice")
    stt, tts = parts["stt"], parts["tts"]
    await stt.load()
    tts = await load_tts(tts, cfg)
    gate = build_wake_gate(cfg) if settings.mode == "wake_word" else None
    return VoiceEngines(parts["audio_in"], parts["audio_out"], stt, tts, gate)


EngineLoader = Callable[[VoiceSettings, Path], Awaitable[VoiceEngines]]
#: Sends one request to the core and returns its answer (None: not connected / no answer).
AskCore = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
#: Sends one notification to the core.
TellCore = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class _Turn:
    started: float
    language: str
    seq: int = -1
    done: bool = False


@dataclass
class _Speech:
    turn_id: str
    text: str
    language: str
    done: asyncio.Future[bool] | None = None


def earcon_pcm(sample_rate: int, *, freq: float = 880.0, ms: int = 90) -> bytes:
    """A short sine tone with soft edges, PCM16 mono."""
    import numpy as np  # noqa: PLC0415

    count = max(1, int(sample_rate * ms / 1000))
    t = np.arange(count) / sample_rate
    envelope = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)
    wave = 0.25 * envelope * np.sin(2 * math.pi * freq * t)
    return bytes((wave * 32767).astype("<i2").tobytes())


@dataclass
class VoiceSatellite:
    home: Path
    settings: Callable[[], VoiceSettings]
    #: The group `voice` is enabled here, not narrowed, the node not stopped, and the core
    #: understands satellites - read before every decision.
    allowed: Callable[[], bool]
    ask_core: AskCore
    tell_core: TellCore
    #: Stops this node as `nox node stop` does (the spoken kill phrase).
    stop_here: Callable[[str], None]
    audit: Callable[[str, str], None] = lambda _action, _result: None
    load: EngineLoader = load_engines
    clock: Callable[[], float] = time.monotonic
    #: The core's word on the microphone (`nox/voice/gate`); closed until it says otherwise.
    core_capture: bool = False
    core_reason: str = "not connected"
    connected: bool = False
    engines: VoiceEngines | None = None
    pipeline: Any = None
    turns: dict[str, _Turn] = field(default_factory=dict)
    last_error: str = ""
    _queue: asyncio.Queue[_Speech] | None = None
    _speaker: asyncio.Task[None] | None = None
    _speaking_turn: str = ""
    _last_level: float = 0.0
    _last_level_at: float = 0.0
    _was_open: bool = False
    _ptt_held: bool = False
    #: Until when the microphone stays closed after this device last spoke.
    _quiet_from: float = 0.0
    _loading: asyncio.Lock = field(default_factory=asyncio.Lock)
    _background: set[asyncio.Task[Any]] = field(default_factory=set)

    # -- the switches -----------------------------------------------------------------------------

    @property
    def mute_file(self) -> Path:
        return self.home / MUTE_FILE

    @property
    def ptt_file(self) -> Path:
        return self.home / PTT_FILE

    def muted(self) -> bool:
        return self.mute_file.exists()

    def ptt_held(self) -> bool:
        try:
            age = time.time() - self.ptt_file.stat().st_mtime
        except OSError:
            return False
        if age > PTT_STALE_S:
            with contextlib.suppress(OSError):
                self.ptt_file.unlink()
            return False
        return True

    def capture_allowed(self) -> bool:
        """Every condition at once, read fresh (the pipeline asks before every frame). While
        this device speaks - and `ECHO_TAIL_S` after - the microphone hears its own voice:
        nothing is captured then, unless push-to-talk is held (a person's deliberate act)."""
        if not (self.connected and self.core_capture and self.allowed()):
            return False
        return self._ptt_held or not self.echo()

    def echo(self) -> bool:
        """This device is speaking, or stopped a moment ago."""
        pipeline = self.pipeline
        speaking = bool(pipeline is not None and pipeline.speaking)
        return speaking or self.clock() < self._quiet_from

    # -- lifecycle --------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Follow the switches until `stop`: start the pipeline when the group is usable, stop it
        (and free the models) when it is not, push-to-talk and mute from the marker files."""
        try:
            while not stop.is_set():
                try:
                    await self.refresh()
                except Exception as exc:  # noqa: BLE001 - one bad round never ends the satellite
                    self.last_error = f"{type(exc).__name__}"
                    log.warning("node.voice_refresh_failed", error=self.last_error)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), POLL_S)
        finally:
            await self.shutdown()

    async def refresh(self) -> None:
        usable = self.allowed() and self.connected
        if usable and self.pipeline is None:
            await self._start()
        elif not usable and self.pipeline is not None:
            await self.shutdown()
        pipeline = self.pipeline
        if pipeline is not None:
            await pipeline.set_muted(self.muted())
            held = self.ptt_held()
            if held != self._ptt_held:
                self._ptt_held = held
                if held and pipeline.speaking:
                    await pipeline.interrupt(reason="push_to_talk")  # a person wants to speak
                await pipeline.push_to_talk(held)
            await pipeline.refresh_gate()
            is_open = bool(pipeline.capture_active())
            if is_open and not self._was_open and self.settings().mode == "wake_word":
                await self._beep(660.0)  # the microphone opened: heard as well as seen
            self._was_open = is_open
        self._write_state()

    async def _start(self) -> None:
        async with self._loading:
            if self.pipeline is not None:
                return
            settings = self.settings()
            if self.engines is None:
                try:
                    self.engines = await self.load(settings, self.home)
                except Exception as exc:  # noqa: BLE001 - said in the state, retried later
                    self.last_error = f"engines: {type(exc).__name__}"
                    log.warning("node.voice_engines_failed", error=self.last_error)
                    return
            from nox.voice.pipeline import DefaultVoicePipeline, PipelineConfig  # noqa: PLC0415

            engines = self.engines
            config = PipelineConfig(
                wake_word=settings.wake_word,
                language=settings.language,
                listening_mode="ptt_only" if settings.mode == "push_to_talk" else "continuous",
                # Speech without the wake word (or push-to-talk) is never reported at all.
                require_wake_word=True,
                # No barge-in on a speaker device: what the microphone hears while this device
                # speaks is mostly its own voice. Push-to-talk interrupts instead (`refresh`).
                barge_in=False,
            )
            pipeline = DefaultVoicePipeline(
                audio_in=engines.audio_in,
                audio_out=engines.audio_out,
                stt=engines.stt,
                tts=engines.tts,
                emit=self._on_event,
                capture_allowed=self.capture_allowed,
                config=config,
                wake_gate=engines.wake_gate,
            )
            await pipeline.start()
            self.pipeline = pipeline
            self._queue = asyncio.Queue()
            self._speaker = asyncio.create_task(self._speak_loop(), name="nox-node-voice-speak")
            self.last_error = ""
            self.audit("node.voice_on", settings.mode)
            log.info("node.voice_started", mode=settings.mode)

    async def shutdown(self) -> None:
        """Microphone closed, speech stopped, models freed."""
        pipeline, self.pipeline = self.pipeline, None
        speaker, self._speaker = self._speaker, None
        if speaker is not None:
            speaker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await speaker
        if pipeline is not None:
            with contextlib.suppress(Exception):
                await pipeline.stop()
            self.audit("node.voice_off", "")
        queue, self._queue = self._queue, None
        while queue is not None and not queue.empty():
            item = queue.get_nowait()
            if item.done is not None and not item.done.done():
                item.done.set_result(False)
        engines, self.engines = self.engines, None
        if engines is not None:
            await engines.unload()
        self.turns.clear()
        self._was_open = self._ptt_held = False
        self._write_state()

    def stopped(self, _reason: str = "") -> None:
        """A stop on this node (kill, local stop, dead man): nothing more is said."""
        for turn_id in list(self.turns):
            self._cancel(turn_id)
        pipeline = self.pipeline
        if pipeline is not None:
            task = asyncio.get_running_loop().create_task(pipeline.interrupt(reason="stop"))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    def connection(self, connected: bool) -> None:
        """The node's connection to its core came or went (closed: nothing to say anything to)."""
        self.connected = connected
        if not connected:
            self.core_capture, self.core_reason = False, "not connected"
            self.turns.clear()

    # -- what the pipeline says -------------------------------------------------------------------

    async def _on_event(self, name: str, payload: dict[str, Any]) -> None:
        if name == E.VOICE_TRANSCRIPT_READY:
            await self._heard(payload)
        elif name == E.VOICE_KILL_PHRASE:
            # Never text for the core: this device stops, which stops Nox everywhere.
            self.audit("node.voice_kill_phrase", "stopped")
            self.stop_here("Nox, Notaus (voice satellite)")
        elif name == E.VOICE_PTT_PRESSED:
            await self._beep(880.0)
        elif name == E.TTS_LEVEL:
            await self._level(float(payload.get("level", 0.0) or 0.0))
        elif name in (E.TTS_FINISHED, E.TTS_INTERRUPTED):
            self._quiet_from = self.clock() + ECHO_TAIL_S
            await self._level(0.0, force=True)
        elif name == E.TTS_STARTED:
            self._quiet_from = self.clock() + ECHO_TAIL_S

    async def _heard(self, payload: dict[str, Any]) -> None:
        text = str(payload.get("text", "")).strip()
        if not payload.get("addressed_to_nox") or not text or not self.capture_allowed():
            return
        language = str(payload.get("language") or "de")
        language = language if language in ("de", "en") else "de"
        turn_id = uuid.uuid4().hex
        try:
            message = p.VoiceHeard.model_validate(
                {"turn_id": turn_id, "text": text[: p.MAX_HEARD_CHARS], "language": language}
            )
        except ValidationError:
            return
        # A new question: the answer to the last one is not wanted any more.
        for old in list(self.turns):
            self._cancel(old)
        self.turns[turn_id] = _Turn(self.clock(), language)
        while len(self.turns) > MAX_TURNS:
            self.turns.pop(next(iter(self.turns)))
        answer = await self.ask_core(p.M_VOICE_HEARD, message.model_dump())
        result = answer.get("result") if isinstance(answer, dict) else None
        accepted = isinstance(result, dict) and result.get("accepted") is True
        if self.pipeline is not None:
            # One utterance per wake word: the next one needs the wake word (or push-to-talk)
            # again - no open window an echo or a passer-by could use.
            self.pipeline.wake_gate.reset()
        # What was heard is never recorded here - only that it was, and how long it was.
        self.audit("node.voice_heard", f"{len(text)} chars, {'sent' if accepted else 'refused'}")
        if not accepted:
            self.turns.pop(turn_id, None)
            reason = result.get("reason") if isinstance(result, dict) else "no_answer"
            log.info("node.voice_refused_by_core", reason=str(reason)[:64])
            return
        await self._beep(1320.0, ms=60)

    async def _level(self, level: float, *, force: bool = False) -> None:
        if not math.isfinite(level):
            return
        level = round(min(1.0, max(0.0, level)), 3)
        now = self.clock()
        if not force and now - self._last_level_at < 1.0 / LEVEL_HZ and level != 0.0:
            return
        if level == self._last_level and not force:
            return
        self._last_level, self._last_level_at = level, now
        with contextlib.suppress(Exception):
            await self.tell_core(p.M_VOICE_LEVEL, {"level": level, "turn_id": self._speaking_turn})

    async def _beep(self, freq: float, *, ms: int = 90) -> None:
        engines = self.engines
        if not self.settings().earcon or engines is None or self.pipeline is None:
            return
        if self.pipeline.speaking:
            return
        rate = int(getattr(engines.tts, "sample_rate", 16000) or 16000)
        pcm = earcon_pcm(rate, freq=freq, ms=ms)

        async def chunks() -> AsyncIterator[bytes]:
            yield pcm

        from nox.voice.base import Channel  # noqa: PLC0415

        with contextlib.suppress(Exception):
            await engines.audio_out.play(chunks(), rate, Channel.PRIVATE, utterance_id="earcon")

    # -- what the core says -----------------------------------------------------------------------

    def _live(self, turn_id: str) -> _Turn | None:
        turn = self.turns.get(turn_id)
        if turn is None or turn.done or self.clock() - turn.started > TURN_TTL_S:
            return None
        return turn

    async def on_say(self, params: dict[str, Any]) -> None:
        """`nox/voice/say`: one sentence for a question this device asked - nothing else."""
        try:
            say = p.VoiceSay.model_validate(params)
        except ValidationError:
            return
        turn = self._live(say.turn_id)
        if not self.allowed():
            return  # stopped or switched off meanwhile: nothing is said
        if turn is None or say.seq <= turn.seq or say.seq >= MAX_SENTENCES:
            return  # not asked here, over, out of order or too long: never spoken
        turn.seq = say.seq
        if say.last:
            turn.done = True
        if say.text.strip() and self._queue is not None:
            self._queue.put_nowait(_Speech(say.turn_id, say.text.strip(), say.language))

    async def on_cancel(self, params: dict[str, Any]) -> None:
        try:
            cancel = p.VoiceCancel.model_validate(params)
        except ValidationError:
            return
        if cancel.turn_id:
            self._cancel(cancel.turn_id)
        else:
            for turn_id in list(self.turns):
                self._cancel(turn_id)
        if self.pipeline is not None:
            await self.pipeline.interrupt(reason="core_cancel")

    async def on_gate(self, params: dict[str, Any]) -> None:
        try:
            gate = p.VoiceGate.model_validate(params)
        except ValidationError:
            return
        self.core_capture, self.core_reason = gate.capture, gate.reason
        if self.pipeline is not None:
            await self.pipeline.refresh_gate()
        self._write_state()

    def _cancel(self, turn_id: str) -> None:
        turn = self.turns.pop(turn_id, None)
        if turn is not None:
            turn.done = True
        queue = self._queue
        if queue is None:
            return
        kept: list[_Speech] = []
        while not queue.empty():
            item = queue.get_nowait()
            if item.turn_id == turn_id:
                if item.done is not None and not item.done.done():
                    item.done.set_result(False)
            else:
                kept.append(item)
        for item in kept:
            queue.put_nowait(item)

    async def say(self, text: str, language: str) -> bool:
        """`voice.say`: say `text` here (the model chose to). True once it was played."""
        if self.pipeline is None or self._queue is None:
            raise NodeRefusal("voice_not_running", "the voice satellite is not running here")
        done: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait(_Speech("", text, language, done))
        return await done

    async def _speak_loop(self) -> None:
        from nox.voice.base import Channel, TtsRequest  # noqa: PLC0415

        assert self._queue is not None
        queue = self._queue
        while True:
            item = await queue.get()
            if item.turn_id and item.turn_id not in self.turns:
                continue  # cancelled while it waited
            pipeline = self.pipeline
            if pipeline is None:
                if item.done is not None and not item.done.done():
                    item.done.set_result(False)
                continue
            self._speaking_turn = item.turn_id
            ok = False
            try:
                await pipeline.say(
                    TtsRequest(
                        utterance_id=f"node:{uuid.uuid4().hex[:12]}",
                        text=item.text,
                        language=item.language,
                        channel=Channel.PRIVATE,  # this device's speaker, never a stream
                    )
                )
                ok = True
            except Exception as exc:  # noqa: BLE001 - one sentence that fails is skipped
                log.warning("node.voice_say_failed", error=type(exc).__name__)
            finally:
                self._speaking_turn = ""
                if item.done is not None and not item.done.done():
                    item.done.set_result(ok)

    # -- what the tray shows ----------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        pipeline = self.pipeline
        return {
            "running": pipeline is not None,
            "mic_open": bool(pipeline is not None and pipeline.capture_active()),
            "speaking": bool(pipeline is not None and pipeline.speaking),
            "muted": self.muted(),
            "mode": self.settings().mode,
            "core_allows": self.core_capture,
            "core_reason": self.core_reason,
            "error": self.last_error,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }

    def _write_state(self) -> None:
        with contextlib.suppress(OSError):
            (self.home / STATE_FILE).write_text(json.dumps(self.state()), encoding="utf-8")


def _plain(text: str) -> list[str]:
    """Words, case- and diacritics-insensitive ("NÖX" -> "nox")."""
    import unicodedata  # noqa: PLC0415

    from nox.voice.stt.wake_word import normalize  # noqa: PLC0415

    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return normalize(stripped).split()


def says_a_command(text: str, wake_word: str = "Nox") -> bool:
    """Whether `text`, spoken here, could address Nox: the wake word (or a spelling of it)
    anywhere, or a kill phrase. Nox never says that itself (`voice.say`): its own voice would
    come back through a microphone as a command."""
    from nox.voice.stt.wake_word import KILL_PHRASES, is_wake_token  # noqa: PLC0415

    names = {w for w in _plain(wake_word)} or {"nox"}
    words = _plain(text)
    if any(is_wake_token(word, name=name) for word in words for name in names):
        return True
    return any(
        tuple(words[i : i + len(phrase)]) == phrase
        for phrase in KILL_PHRASES
        for i in range(len(words))
    )


class SayArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(
        min_length=1, max_length=300, description="What to say, one or two sentences."
    )
    language: Literal["de", "en"] = "de"


def voice_tools(satellite: VoiceSatellite) -> dict[str, NodeTool]:
    """`voice.say` on this device (group `voice`)."""

    async def say(args: BaseModel) -> dict[str, Any]:
        assert isinstance(args, SayArgs)
        spoken = await satellite.say(args.text, args.language)
        return {"spoken": spoken}

    def precheck(args: BaseModel) -> str | None:
        assert isinstance(args, SayArgs)
        wake = satellite.settings().wake_word
        return "voice_say_command_words" if says_a_command(args.text, wake) else None

    tool = NodeTool(
        "voice.say",
        "voice",
        "low",
        "Say something out loud on this device's speaker (in Nox's voice).",
        SayArgs,
        say,
        path_safe=True,  # opens no path
        precheck=precheck,  # never the wake word or a kill phrase: it would come back as one
    )
    return {tool.name: tool}
