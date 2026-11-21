"""What the voice satellite is set to and what this device can do: the marker files in the node's
folder, `VoiceSettings`, the listening mode and the check for the speech libraries."""

from __future__ import annotations

import platform
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Marker files in the node's folder: muted here (only this device removes it), push-to-talk held
#: (written by the tray's key or `nox node voice talk`), and what the tray shows.
MUTE_FILE = "voice-muted"


PTT_FILE = "voice-ptt"


STATE_FILE = "voice.json"


class VoiceSettings(BaseModel):
    """The `voice:` part of this node's `satellites.yaml`. How it listens and speaks - not
    whether: that is the group `voice` (`nox node allow voice`)."""

    model_config = ConfigDict(extra="forbid")

    #: `auto`: push-to-talk with a screen, the wake word without one (`listening_mode`).
    mode: Literal["auto", "push_to_talk", "wake_word"] = "auto"
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


def listening_mode(
    settings: VoiceSettings, screen: Callable[[], bool] | None = None
) -> Literal["push_to_talk", "wake_word"]:
    """How this device listens: the setting, or for `auto` push-to-talk where there is a screen
    (a key to hold, a tray) and the wake word where there is none - read when asked, so a desktop
    session that starts later counts."""
    if settings.mode != "auto":
        return settings.mode
    if screen is None:
        from nox.node.device.confirm import has_screen  # noqa: PLC0415 - resolved at call time, so a replaced probe is seen

        screen = has_screen
    return "push_to_talk" if screen() else "wake_word"


def voice_support(has_module: Callable[[str], bool] | None = None) -> str | None:
    """Why the voice satellite cannot work on this device (a library is missing), or None."""
    if has_module is None:
        has_module = installed_module()
    if not has_module("sounddevice"):
        return "the microphone and speaker need the extra `voice` (sounddevice)"
    if has_module is installed_module() and not _portaudio_there():
        return (
            "the microphone and speaker need the PortAudio library "
            "(Linux: sudo apt install libportaudio2)"
        )
    if not has_module("faster_whisper"):
        return "speech recognition needs the extra `voice` (faster-whisper)"
    if not (has_module("supertonic") or has_module("piper")):
        return "speaking needs the extra `voice-supertonic` or `voice` (Piper)"
    return None


def installed_module() -> Callable[[str], bool]:
    # Loaded here: the world package holds the capture, browser and desktop tools, which the
    # speech setup commands and the tray's state reads never need (start time on a small device).
    from nox.node.device.world.node_world import installed  # noqa: PLC0415

    return installed


def _portaudio_there() -> bool:
    """sounddevice imports only with the system's PortAudio library behind it."""
    try:
        import sounddevice  # noqa: F401, PLC0415
    except OSError:
        return False
    except ImportError:
        return True  # not installed: said by the check before
    return True


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
                "listening_mode": "ptt_only"
                if listening_mode(settings) == "push_to_talk"
                else "continuous",
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
