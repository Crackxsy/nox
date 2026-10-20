"""Where voice model files live, and how the user gets the Kokoro and Supertonic ones.

Nox never downloads a model without the user's go-ahead. Engines resolve their files below a models
root and, when a file is missing, report `unavailable` with the exact path plus the ways to fetch
it. Every download is one the user started: the "Voice" tile in the dashboard's settings (the click
is the consent; the core then runs `python -m nox.worker --download-supertonic --progress-json`),
the optional step in `nox onboard`, `nox voice setup`, or the explicit `nox voice download-kokoro`
/ `nox voice download-supertonic` (`python -m nox.worker --download-kokoro` /
`--download-supertonic`).
The privacy modes `offline` and `private` refuse the dashboard, onboarding and `nox voice setup`
downloads before anything is fetched, and switching to one of them stops a dashboard download that
is already running (`nox.voice.service`). Every file is pinned to an immutable revision and its
SHA-256, the stream is cut off past the expected size, and an existing file is kept only when it
still matches its pin (`download_files`).

Resolution order for the models root:
1. `voice.models_dir` from the configuration, when set; 2. `$NOX_DATA_DIR/models`, when the process
was started with that variable (the core starts its workers with it); 3. `models` in Nox's folder
(`nox_home()`), which mirrors the `paths.data_dir` default in `config/defaults.yaml`. A deployment
that moved `paths.data_dir` elsewhere and starts the worker by hand sets `voice.models_dir`
explicitly - the voice worker only ever receives the `voice` section of the configuration.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from nox.paths import nox_home
from nox.voice._logging import get_logger

log = get_logger(__name__)

KOKORO_DOWNLOAD_HINT = (
    "download them in the dashboard (Settings -> Voice) or run `nox voice setup` "
    "(~354 MB, Apache-2.0 model files)"
)


class ModelFile(NamedTuple):
    """One downloadable model file: where it comes from, its size and (when pinned) its SHA-256."""

    url: str
    size: int
    sha256: str = ""


#: Kokoro v1.0 (the `kokoro-onnx` model files, Apache-2.0), pinned to one Hugging Face commit of a
#: mirror whose files are byte-identical to the upstream GitHub release `model-files-v1.0` of
#: thewh1teagle/kokoro-onnx (same size, same SHA-256). A release asset can be replaced in place; a
#: commit cannot, so the download is reproducible and every file is checked against its SHA-256.
KOKORO_REPO = "fastrtc/kokoro-onnx"
KOKORO_REVISION = "8d07950c9b6c87ce6809e9bba7bd494336217c2a"
_KOKORO_BASE = f"https://huggingface.co/{KOKORO_REPO}/resolve/{KOKORO_REVISION}"
_KOKORO_PINS: dict[str, tuple[int, str]] = {
    "kokoro-v1.0.onnx": (
        325_532_387,
        "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    ),
    "voices-v1.0.bin": (
        28_214_398,
        "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
    ),
}
KOKORO_FILES: dict[str, ModelFile] = {
    name: ModelFile(f"{_KOKORO_BASE}/{name}", size, digest)
    for name, (size, digest) in _KOKORO_PINS.items()
}

#: Supertonic 3 (Supertone Inc.), pinned to one Hugging Face commit so a download is reproducible
#: and every file is checked against its SHA-256. The layout below the target directory mirrors the
#: upstream repository (`onnx/`, `voice_styles/`), which is what the `supertonic` package loads.
#: The weights are licensed under BigScience OpenRAIL-M - permissive and commercial use is allowed,
#: but with use-based restrictions (see NOTICE); `LICENSE` is fetched next to the model on purpose.
SUPERTONIC_REPO = "Supertone/supertonic-3"
SUPERTONIC_REVISION = "724fb5abbf5502583fb520898d45929e62f02c0b"
_SUPERTONIC_BASE = f"https://huggingface.co/{SUPERTONIC_REPO}/resolve/{SUPERTONIC_REVISION}"
_SUPERTONIC_PINS: dict[str, tuple[int, str]] = {
    "onnx/duration_predictor.onnx": (
        3_700_147,
        "c3eb91414d5ff8a7a239b7fe9e34e7e2bf8a8140d8375ffb14718b1c639325db",
    ),
    "onnx/text_encoder.onnx": (
        36_416_150,
        "c7befd5ea8c3119769e8a6c1486c4edc6a3bc8365c67621c881bbb774b9902ff",
    ),
    "onnx/vector_estimator.onnx": (
        256_534_781,
        "883ac868ea0275ef0e991524dc64f16b3c0376efd7c320af6b53f5b780d7c61c",
    ),
    "onnx/vocoder.onnx": (
        101_424_195,
        "085de76dd8e8d5836d6ca66826601f615939218f90e519f70ee8a36ed2a4c4ba",
    ),
    "onnx/tts.json": (8_253, "42078d3aef1cd43ab43021f3c54f47d2d75ceb4e75f627f118890128b06a0d09"),
    "onnx/unicode_indexer.json": (
        277_676,
        "9bf7346e43883a81f8645c81224f786d43c5b57f3641f6e7671a7d6c493cb24f",
    ),
    "voice_styles/F1.json": (
        292_046,
        "bbdec6ee00231c2c742ad05483df5334cab3b52fda3ba38e6a07059c4563dbc2",
    ),
    "voice_styles/F2.json": (
        292_423,
        "7c722c6a72707b1a77f035d67f0d1351ba187738e06f7683e8c72b1df3477fc6",
    ),
    "voice_styles/F3.json": (
        290_794,
        "12f6ef2573baa2defa1128069cb59f203e3ab67c92af77b42df8a0e3a2f7c6ab",
    ),
    "voice_styles/F4.json": (
        291_808,
        "c2fa764c1225a76dfc3e2c73e8aa4f70d9ee48793860eb34c295fff01c2e032b",
    ),
    "voice_styles/F5.json": (
        291_479,
        "45966e73316415626cf41a7d1c6f3b4c70dbc1ba2bee5c1978ef0ce33244fc8d",
    ),
    "voice_styles/M1.json": (
        291_748,
        "e35604687f5d23694b8e91593a93eec0e4eca6c0b02bb8ed69139ab2ea6b0a5b",
    ),
    "voice_styles/M2.json": (
        292_055,
        "b76cbf62bac707c710cf0ae5aba5e31eea1a6339a9734bfae33ab98499534a50",
    ),
    "voice_styles/M3.json": (
        290_198,
        "ea1ac35ccb91b0d7ecad533a2fbd0eec10c91513d8951e3b25fbba99954e159b",
    ),
    "voice_styles/M4.json": (
        291_522,
        "ca8eefad4fcd989c9379032ff3e50738adc547eeb5e221b82593a6d7b3bac303",
    ),
    "voice_styles/M5.json": (
        291_469,
        "dd22b92740314321f8ae11c5e87f8dd60d060f15dd3a632b5adf77f471f77af2",
    ),
    "LICENSE": (15_007, "0d944a9110fed9a9602d60e0423a272903e7bd21ab060490774efc77c2275e9f"),
}
SUPERTONIC_FILES: dict[str, ModelFile] = {
    name: ModelFile(f"{_SUPERTONIC_BASE}/{name}", size, digest)
    for name, (size, digest) in _SUPERTONIC_PINS.items()
}
#: What the engine cannot start without; voice styles are checked per configured voice instead.
SUPERTONIC_REQUIRED: tuple[str, ...] = tuple(n for n in SUPERTONIC_FILES if n.startswith("onnx/"))
SUPERTONIC_VOICES: tuple[str, ...] = tuple(
    Path(n).stem for n in SUPERTONIC_FILES if n.startswith("voice_styles/")
)
SUPERTONIC_DOWNLOAD_HINT = (
    "download them in the dashboard (Settings -> Voice) or run `nox voice setup` "
    "(~400 MB, OpenRAIL-M model files, see NOTICE)"
)

#: Engines whose model files Nox can fetch itself. Piper's voices are not among them.
DOWNLOADABLE_ENGINES: tuple[str, ...] = ("supertonic", "kokoro")


def kokoro_files() -> dict[str, ModelFile]:
    return dict(KOKORO_FILES)


def engine_files(engine: str) -> dict[str, ModelFile]:
    """The files `engine` needs below `<models root>/<engine>`; empty for an engine Nox cannot
    fetch (Piper)."""
    if engine == "supertonic":
        return dict(SUPERTONIC_FILES)
    if engine == "kokoro":
        return kokoro_files()
    return {}


def files_size(files: dict[str, ModelFile]) -> int:
    return sum(source.size for source in files.values())


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """Where a running `download_files` is: the current file and the whole run, in bytes.

    `total` counts only the files this run actually fetches (present ones are skipped), so a
    resumed download does not start at 40 %.
    """

    file: str
    file_done: int
    file_total: int
    done: int
    total: int
    index: int  # 1-based number of the current file among the fetched ones
    count: int  # how many files this run fetches

    @property
    def fraction(self) -> float:
        return min(1.0, self.done / self.total) if self.total > 0 else 0.0


ProgressFn = Callable[[DownloadProgress], None]


def default_data_dir() -> Path:
    """`paths.data_dir` as far as the voice worker can know it (see the module docstring)."""
    data_dir = os.environ.get("NOX_DATA_DIR", "").strip()
    if data_dir:
        return Path(os.path.expandvars(data_dir)).expanduser()
    return nox_home()


def default_models_root() -> Path:
    return default_data_dir() / "models"


def default_clips_dir() -> Path:
    """`<data_dir>/data/clips`, matching `clips.library_root` and friends in the core config."""
    return default_data_dir() / "data" / "clips"


def models_root(configured: str | os.PathLike[str] | None = None) -> Path:
    if configured:
        return Path(os.path.expandvars(str(configured))).expanduser()
    return default_models_root()


def engine_models_dir(engine: str, configured: str | os.PathLike[str] | None = None) -> Path:
    """`<models root>/<engine>`, e.g. `.../models/kokoro` or `.../models/openwakeword`."""
    return models_root(configured) / engine


def missing_files(directory: Path, names: tuple[str, ...]) -> list[str]:
    return [n for n in names if not (directory / n).is_file()]


#: How far a stream may run past a file's expected size before it is cut off. The pinned sizes are
#: exact, so this is only a sanity margin: a server that keeps sending (by accident or on purpose)
#: cannot fill the disk.
SIZE_MARGIN_BYTES = 64 * 1024

#: `download_files` exit codes besides 0: failed, and stopped by `cancel` (2 means "an engine Nox
#: cannot fetch", see `download_engine`).
EXIT_FAILED = 1
EXIT_ABORTED = 3

#: Asked before every file and after every received chunk; a non-empty string stops the download
#: and is the reason (e.g. the privacy mode that no longer allows it).
CancelFn = Callable[[], str | None]


class DownloadAbortedError(Exception):
    """`cancel` asked a running download to stop."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def max_download_bytes(source: ModelFile) -> int | None:
    """The most bytes a download of `source` may receive; None when no size is known."""
    return source.size + SIZE_MARGIN_BYTES if source.size > 0 else None


def file_matches(path: Path, source: ModelFile) -> bool:
    """Whether `path` is a complete copy of `source`: for a pinned file the exact size and the
    SHA-256, otherwise (no pin) merely a non-empty file."""
    if not path.is_file():
        return False
    size = path.stat().st_size
    if size <= 0:
        return False
    if not source.sha256:
        return True
    return size == source.size and _sha256(path) == source.sha256


def _declared_length(response: object) -> int | None:
    headers = getattr(response, "headers", None)
    value = headers.get("content-length") if headers is not None else None
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _check_cancel(cancel: CancelFn | None) -> None:
    reason = cancel() if cancel is not None else None
    if reason:
        raise DownloadAbortedError(reason)


def download_files(
    target_dir: Path,
    files: dict[str, ModelFile],
    *,
    echo: Callable[[str], None] = print,  # CLI helper; the caller passes typer.echo
    force: bool = False,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> int:
    """Fetch `files` (path relative to `target_dir` -> source). User-initiated only.

    Streams to a `.part` file and renames on success, so an interrupted download never leaves a
    half-written model behind that the engine would then load. The stream is cut off as soon as it
    runs past the expected size (plus `SIZE_MARGIN_BYTES`); a file with a pinned SHA-256 is hashed
    while it streams and discarded on a mismatch. A file that already exists is kept only when it
    matches its pin - a corrupted or swapped one is reported and fetched again. `progress`, when
    given, is called after every received chunk (about 1 MB) with the current file's and the whole
    run's byte counts; `cancel`, when given, can stop the run between two chunks (`EXIT_ABORTED`,
    nothing of the current file is kept). Returns a process exit code.
    """
    import httpx

    target_dir.mkdir(parents=True, exist_ok=True)
    todo: list[tuple[str, ModelFile]] = []
    for name, source in files.items():
        target = target_dir / name
        if not force and target.is_file():
            if file_matches(target, source):
                echo(f"exists   {target}")
                continue
            echo(f"mismatch {target}: not the pinned size/SHA-256, fetching it again")
        todo.append((name, source))
    total = files_size(dict(todo))
    finished = 0  # bytes of the files this run has already completed
    for index, (name, source) in enumerate(todo, start=1):
        target = target_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        limit = max_download_bytes(source)
        echo(f"fetching {source.url}\n      -> {target} ({source.size / 1e6:.1f} MB)")
        done = 0
        try:
            _check_cancel(cancel)
            digest = hashlib.sha256()
            with httpx.stream("GET", source.url, follow_redirects=True, timeout=120.0) as response:
                response.raise_for_status()
                declared = _declared_length(response)
                if limit is not None and declared is not None and declared > limit:
                    raise ValueError(f"server announces {declared} bytes, expected {source.size}")
                with tmp.open("wb") as handle:
                    for chunk in response.iter_bytes(1 << 20):
                        done += len(chunk)
                        if limit is not None and done > limit:
                            raise ValueError(
                                f"stream exceeds the expected {source.size} bytes; aborted"
                            )
                        handle.write(chunk)
                        digest.update(chunk)
                        if progress is not None:
                            progress(
                                DownloadProgress(
                                    file=name,
                                    file_done=done,
                                    file_total=source.size,
                                    done=finished + done,
                                    total=max(total, finished + done),
                                    index=index,
                                    count=len(todo),
                                )
                            )
                        _check_cancel(cancel)
            if source.sha256 and digest.hexdigest() != source.sha256:
                raise ValueError("SHA-256 does not match the pinned value; file discarded")
            if source.size > 0 and done != source.size:
                raise ValueError(f"received {done} bytes, expected {source.size}; file discarded")
        except DownloadAbortedError as exc:
            tmp.unlink(missing_ok=True)
            echo(f"aborted  {name}: {exc}")
            return EXIT_ABORTED
        except Exception as exc:  # noqa: BLE001 - a CLI download reports, it does not traceback
            tmp.unlink(missing_ok=True)
            echo(f"failed   {name}: {type(exc).__name__}: {exc}")
            return EXIT_FAILED
        tmp.replace(target)
        finished += done
        echo(f"done     {name} ({done / 1e6:.1f} MB)")
    return 0


def download_kokoro(
    target_dir: Path,
    *,
    echo: Callable[[str], None] = print,
    force: bool = False,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> int:
    """Fetch the Kokoro v1.0 model files into `target_dir`. User-initiated only."""
    code = download_files(
        target_dir, kokoro_files(), echo=echo, force=force, progress=progress, cancel=cancel
    )
    if code == 0:
        echo(f"Kokoro model files are in {target_dir}. Set `voice.tts.engine: kokoro` to use them.")
    return code


def download_supertonic(
    target_dir: Path,
    *,
    echo: Callable[[str], None] = print,
    force: bool = False,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> int:
    """Fetch the pinned Supertonic 3 model files into `target_dir`. User-initiated only."""
    echo(
        "Supertonic 3 model files are licensed under BigScience OpenRAIL-M: free to use, but with "
        "use-based restrictions (e.g. disclose that the voice is machine-generated) - see NOTICE."
    )
    code = download_files(
        target_dir, SUPERTONIC_FILES, echo=echo, force=force, progress=progress, cancel=cancel
    )
    if code == 0:
        echo(
            f"Supertonic model files are in {target_dir}. Nox speaks with them from its next "
            "start (`voice.tts.engine: supertonic`, the default)."
        )
    return code


def download_engine(
    engine: str,
    target_dir: Path,
    *,
    echo: Callable[[str], None] = print,
    force: bool = False,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
) -> int:
    """`download_supertonic` or `download_kokoro` by engine name; 2 for an engine Nox cannot
    fetch."""
    if engine == "supertonic":
        return download_supertonic(
            target_dir, echo=echo, force=force, progress=progress, cancel=cancel
        )
    if engine == "kokoro":
        return download_kokoro(target_dir, echo=echo, force=force, progress=progress, cancel=cancel)
    echo(f"Nox does not download model files for the {engine!r} engine.")
    return 2
