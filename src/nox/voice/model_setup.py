"""Getting the voice model files onto the machine once the user has said yes.

Three ways in, one set of rules:

* the "Voice" tile in the dashboard's settings: the core's `VoiceModelService`
  (`nox.voice.service`) runs `python -m nox.worker --download-supertonic --progress-json` as a
  subprocess and reads the JSON lines this module writes (`JsonProgress`) - the core itself may
  only reach the network through the egress guard, whose allow-lists do not include the model host;
* the optional step in `nox onboard`, and
* `nox voice setup`, both through `run_voice_setup` in this process.

Each one refuses in the privacy modes `offline` and `private` before anything is fetched, and a
switch to one of them stops a download that is already running (the core terminates the dashboard's
downloader process; an in-process run checks `privacy_mode_now` between chunks). None of them ever
runs without the user's go-ahead. Which files are meant is decided once, here: the
configured engine's when Nox can fetch them (Supertonic, Kokoro), otherwise Supertonic's - the
default engine, which is what a user looking at the tile with Piper configured would switch to.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nox.voice.models import (
    DOWNLOADABLE_ENGINES,
    CancelFn,
    DownloadProgress,
    ProgressFn,
    download_engine,
    engine_files,
    files_size,
    models_root,
)

__all__ = [
    "BLOCKING_PRIVACY_MODES",
    "MODEL_LICENSES",
    "JsonProgress",
    "ModelStatus",
    "configured_models_root",
    "download_allowed",
    "download_with_json_progress",
    "model_engine",
    "model_status",
    "parse_progress_line",
    "privacy_cancel",
    "run_onboarding_voice_step",
    "run_voice_setup",
    "texts",
]

#: Privacy modes in which no model is downloaded (the same two that stop an Ollama pull).
BLOCKING_PRIVACY_MODES: frozenset[str] = frozenset({"offline", "private"})

#: The license each downloadable model comes under, as shown next to the download.
MODEL_LICENSES: dict[str, str] = {"supertonic": "OpenRAIL-M", "kokoro": "Apache-2.0"}


def download_allowed(privacy_mode: str) -> bool:
    return privacy_mode not in BLOCKING_PRIVACY_MODES


def privacy_cancel(privacy_mode: Callable[[], str]) -> CancelFn:
    """A `cancel` hook for `download_files` that stops the download as soon as the privacy mode
    no longer allows one."""

    def cancel() -> str | None:
        mode = privacy_mode()
        return None if download_allowed(mode) else f"privacy mode {mode!r} allows no downloads"

    return cancel


def model_engine(configured_engine: str) -> str:
    """The engine whose files the setup is about (see the module docstring)."""
    return configured_engine if configured_engine in DOWNLOADABLE_ENGINES else "supertonic"


def configured_models_root(models_dir: str, data_dir: str | os.PathLike[str] | None) -> Path:
    """The models root the core's voice worker will read: `voice.models_dir` when set, otherwise
    `<paths.data_dir>/models` - the core starts its workers with `NOX_DATA_DIR=<paths.data_dir>`."""
    if models_dir:
        return models_root(models_dir)
    if data_dir:
        return Path(os.path.expandvars(str(data_dir))).expanduser() / "models"
    return models_root(None)


@dataclass(frozen=True, slots=True)
class ModelStatus:
    """Which model files exist for one engine, and what is still missing."""

    engine: str
    configured_engine: str
    directory: Path
    files: int
    missing: tuple[str, ...]
    size_bytes: int
    missing_bytes: int
    license: str

    @property
    def present(self) -> bool:
        return not self.missing

    def to_payload(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "configured_engine": self.configured_engine,
            "directory": str(self.directory),
            "files": self.files,
            "missing": list(self.missing),
            "present": self.present,
            "size_bytes": self.size_bytes,
            "missing_bytes": self.missing_bytes,
            "license": self.license,
        }


def model_status(configured_engine: str, root: Path) -> ModelStatus:
    engine = model_engine(configured_engine)
    files = engine_files(engine)
    directory = root / engine
    missing = {
        name: source
        for name, source in files.items()
        if not ((directory / name).is_file() and (directory / name).stat().st_size > 0)
    }
    return ModelStatus(
        engine=engine,
        configured_engine=configured_engine,
        directory=directory,
        files=len(files),
        missing=tuple(missing),
        size_bytes=files_size(files),
        missing_bytes=files_size(missing),
        license=MODEL_LICENSES.get(engine, ""),
    )


# ---- the worker side of a dashboard download ----------------------------------------------------


def _stdout_line(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


@dataclass(slots=True)
class JsonProgress:
    """Writes one JSON object per line for the core: `log`, `progress` (at most every
    `min_interval_s`, and always for a file's last chunk) and a closing `result`."""

    write: Callable[[str], Any] = _stdout_line
    min_interval_s: float = 0.25
    clock: Callable[[], float] = time.monotonic
    _last: float = field(default=float("-inf"))

    def _emit(self, payload: dict[str, Any]) -> None:
        self.write(json.dumps(payload, ensure_ascii=False))

    def log(self, text: str) -> None:
        self._emit({"type": "log", "text": text})

    def progress(self, p: DownloadProgress) -> None:
        now = self.clock()
        if now - self._last < self.min_interval_s and p.file_done < p.file_total:
            return
        self._last = now
        self._emit(
            {
                "type": "progress",
                "file": p.file,
                "file_done": p.file_done,
                "file_total": p.file_total,
                "done": p.done,
                "total": p.total,
                "index": p.index,
                "count": p.count,
            }
        )

    def result(self, code: int) -> None:
        self._emit({"type": "result", "code": code})


def download_with_json_progress(engine: str, target_dir: Path, out: JsonProgress) -> int:
    """`python -m nox.worker --download-<engine> --progress-json`."""
    code = download_engine(engine, target_dir, echo=out.log, progress=out.progress)
    out.result(code)
    return code


def parse_progress_line(line: str) -> dict[str, Any] | None:
    """One line from the worker -> its JSON object, or None for anything else (a stray print)."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) and isinstance(value.get("type"), str) else None


# ---- `nox voice setup` and the step in `nox onboard` --------------------------------------------

TEXTS: dict[str, dict[str, str]] = {
    "de": {
        "intro": "Sprachmodell {engine}: {files} Dateien, etwa {mb} MB, nach {dir}",
        "license_supertonic": (
            "Lizenz der Modelldateien: OpenRAIL-M. Sie verlangt, offenzulegen, dass die Stimme "
            "KI-erzeugt ist - Nox sagt das im Stream von sich aus (stream.ai_voice_disclosure)."
        ),
        "license_kokoro": "Lizenz der Modelldateien: Apache-2.0.",
        "present": "Die Modelldateien sind schon da ({dir}).",
        "not_downloadable": (
            "Für die Engine „{engine}“ lädt Nox keine Dateien herunter; "
            "sie gehören zur Installation."
        ),
        "blocked": (
            "Nicht heruntergeladen: Im Privatsphäre-Modus „{mode}“ lädt Nox nichts aus dem Netz."
        ),
        "ask": "Jetzt herunterladen (etwa {mb} MB)?",
        "declined": (
            "Nicht heruntergeladen. Später: im Dashboard (Einstellungen -> Stimme) oder mit "
            "`nox voice setup`. Bis dahin spricht Nox mit Piper."
        ),
        "progress": "  {pct:>3} %  ({done} von {total} MB)",
        "done": "Fertig. Nox spricht mit der neuen Stimme ab dem nächsten Start.",
        "failed": "Der Download ist fehlgeschlagen (Code {code}). Bis dahin spricht Nox mit Piper.",
    },
    "en": {
        "intro": "Voice model {engine}: {files} files, about {mb} MB, into {dir}",
        "license_supertonic": (
            "Model license: OpenRAIL-M. It requires disclosing that the voice is AI-generated - "
            "Nox does so on stream by default (stream.ai_voice_disclosure)."
        ),
        "license_kokoro": "Model license: Apache-2.0.",
        "present": "The model files are already there ({dir}).",
        "not_downloadable": (
            "Nox does not download files for the {engine!r} engine; they come with the install."
        ),
        "blocked": "Not downloaded: privacy mode {mode!r} allows no downloads.",
        "ask": "Download now (about {mb} MB)?",
        "declined": (
            "Not downloaded. Later: in the dashboard (Settings -> Voice) or with `nox voice "
            "setup`. Until then Nox speaks with Piper."
        ),
        "progress": "  {pct:>3} %  ({done} of {total} MB)",
        "done": "Done. Nox speaks with the new voice from its next start.",
        "failed": "The download failed (code {code}). Until then Nox speaks with Piper.",
    },
}


def texts(language: str | None) -> dict[str, str]:
    lang = (language or "de").lower()[:2]
    return TEXTS.get(lang, TEXTS["en"])


class _Printer:
    """A progress line every `step` percent, so a terminal is not flooded."""

    def __init__(self, t: dict[str, str], echo: Callable[[str], None], step: int = 10) -> None:
        self._t = t
        self._echo = echo
        self._step = step
        self._next = step

    def __call__(self, p: DownloadProgress) -> None:
        pct = int(p.fraction * 100)
        if pct < self._next:
            return
        self._next = (pct // self._step + 1) * self._step
        self._echo(
            self._t["progress"].format(
                pct=pct, done=round(p.done / 1e6), total=round(p.total / 1e6)
            )
        )


DownloadFn = Callable[..., int]


def run_voice_setup(
    *,
    configured_engine: str,
    root: Path,
    privacy_mode: str,
    language: str | None,
    echo: Callable[[str], None],
    confirm: Callable[[str], bool] | None = None,
    download: DownloadFn = download_engine,
    privacy_mode_now: Callable[[], str] | None = None,
) -> int:
    """Download the configured engine's model files with progress lines. Returns an exit code:
    0 when the files are there afterwards (or already were), 1 when the download failed or was
    refused by the privacy mode, 0 when the user declined (an honest "no" is not an error).

    `privacy_mode_now`, when given, is read again after every received chunk: a switch to `offline`
    or `private` while the download runs stops it and keeps nothing of the file in progress.
    """
    t = texts(language)
    if configured_engine not in DOWNLOADABLE_ENGINES:
        echo(t["not_downloadable"].format(engine=configured_engine))
        return 0
    status = model_status(configured_engine, root)
    mb = round(status.missing_bytes / 1e6)
    echo(
        t["intro"].format(
            engine=status.engine,
            files=status.files,
            mb=round(status.size_bytes / 1e6),
            dir=status.directory,
        )
    )
    license_key = f"license_{status.engine}"
    if license_key in t:
        echo(t[license_key])
    if status.present:
        echo(t["present"].format(dir=status.directory))
        return 0
    if not download_allowed(privacy_mode):
        echo(t["blocked"].format(mode=privacy_mode))
        return 1
    if confirm is not None and not confirm(t["ask"].format(mb=mb)):
        echo(t["declined"])
        return 0
    progress: ProgressFn = _Printer(t, echo)
    if privacy_mode_now is None:
        code = download(status.engine, status.directory, echo=echo, progress=progress)
    else:
        code = download(
            status.engine,
            status.directory,
            echo=echo,
            progress=progress,
            cancel=privacy_cancel(privacy_mode_now),
        )
    echo(t["done"] if code == 0 else t["failed"].format(code=code))
    return 0 if code == 0 else 1


def run_onboarding_voice_step(
    language: str | None,
    user_config_path: Path,
    *,
    echo: Callable[[str], None] | None = None,
    ask: Callable[[str, bool], bool] | None = None,
    download: DownloadFn = download_engine,
) -> int:
    """The optional step in `nox onboard`: offer the configured voice's model files.

    Reads the configuration the wizard has just written (models root, privacy mode, engine). The
    question defaults to yes for Supertonic - the default voice, which is not part of the install
    - and to no for Kokoro. Piper needs nothing from here, so nothing is asked.
    """
    import typer  # noqa: PLC0415 - the CLI's own dependency, only for the defaults below

    from nox.entrypoints import build_config  # noqa: PLC0415 - heavy import, one use

    config = build_config(None, user_config_path if user_config_path.is_file() else None)
    engine = str(config.voice.tts.engine)
    if engine not in DOWNLOADABLE_ENGINES:
        return 0
    say = echo or typer.echo
    question = ask or (lambda text, default: typer.confirm(text, default=default))
    say("")
    return run_voice_setup(
        configured_engine=engine,
        root=configured_models_root(config.voice.models_dir, config.paths.data_dir),
        privacy_mode=str(config.privacy.mode),
        language=language,
        echo=say,
        confirm=lambda text: question(text, engine == "supertonic"),
        download=download,
    )
