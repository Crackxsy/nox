"""Where voice model files live, and how the user gets the Kokoro and Whisper ones.

Nox never downloads a model on its own. Engines resolve their files below a models root and, when a
file is missing, report `unavailable` with the exact path plus the command that fetches it - the
decision to pull hundreds of megabytes over the network stays with the user (`nox voice
download-kokoro` / `download-whisper`, i.e. `python -m nox.worker --download-kokoro` /
`--download-whisper [model]`).

Resolution order for the models root:
1. `voice.models_dir` from the configuration, when set; 2. `$NOX_DATA_DIR/models`, when the process
was started with that variable; 3. `<app_dir>/models` (`nox.paths.app_dir`: `%APPDATA%/Nox` on
Windows, the platform's application folder elsewhere), which mirrors the `paths.data_dir` default
in `config/defaults.yaml`. A deployment that moved `paths.data_dir` elsewhere sets
`voice.models_dir` explicitly - the voice worker only ever receives the `voice` section of the
configuration, never `paths`.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from nox.paths import app_dir
from nox.voice._logging import get_logger

log = get_logger(__name__)

#: Model files Kokoro v1.0 needs, with their upstream source and expected size in bytes.
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_FILES: dict[str, tuple[str, int]] = {
    "kokoro-v1.0.onnx": (f"{KOKORO_RELEASE}/kokoro-v1.0.onnx", 325_532_387),
    "voices-v1.0.bin": (f"{KOKORO_RELEASE}/voices-v1.0.bin", 28_214_398),
}
KOKORO_DOWNLOAD_HINT = (
    "run `python -m nox.worker --download-kokoro` (~354 MB, Apache-2.0 model files)"
)


def default_data_dir() -> Path:
    """`paths.data_dir` as far as the voice worker can know it (see the module docstring)."""
    data_dir = os.environ.get("NOX_DATA_DIR", "").strip()
    if data_dir:
        return Path(os.path.expandvars(data_dir)).expanduser()
    return app_dir()


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


def download_kokoro(
    target_dir: Path,
    *,
    echo: Callable[[str], None] = print,  # CLI helper; the caller passes typer.echo
    force: bool = False,
) -> int:
    """Fetch the Kokoro v1.0 model files into `target_dir`. User-initiated only (CLI).

    Streams to a `.part` file and renames on success, so an interrupted download never leaves a
    half-written model behind that the engine would then load. Returns a process exit code.
    """
    import httpx

    target_dir.mkdir(parents=True, exist_ok=True)
    for name, (url, expected) in KOKORO_FILES.items():
        target = target_dir / name
        if target.is_file() and not force:
            if target.stat().st_size == expected:
                echo(f"exists   {target}")
                continue
            echo(f"size mismatch, fetching again: {target}")
        tmp = target.with_suffix(target.suffix + ".part")
        echo(f"fetching {url}\n      -> {target} ({expected / 1e6:.0f} MB)")
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as response:
                response.raise_for_status()
                done = 0
                with tmp.open("wb") as handle:
                    for chunk in response.iter_bytes(1 << 20):
                        handle.write(chunk)
                        done += len(chunk)
        except Exception as exc:  # noqa: BLE001 - a CLI download reports, it does not traceback
            tmp.unlink(missing_ok=True)
            echo(f"failed   {name}: {type(exc).__name__}: {exc}")
            return 1
        if done != expected:
            # A truncated or substituted file must never be loaded as the model.
            tmp.unlink(missing_ok=True)
            echo(f"failed   {name}: got {done} bytes, expected {expected}")
            return 1
        tmp.replace(target)
        echo(f"done     {name} ({done / 1e6:.1f} MB)")
    echo(f"Kokoro model files are in {target_dir}. Set `voice.tts.engine: kokoro` to use them.")
    return 0


# ---- Whisper ------------------------------------------------------------------------------------

#: The file every CTranslate2 Whisper model directory contains; its presence is what "installed"
#: means here.
WHISPER_MODEL_FILE = "model.bin"
_WHISPER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

WhisperDownloader = Callable[[str, Path], None]


def whisper_download_hint(name: str) -> str:
    return f"run `python -m nox.worker --download-whisper {name}` (once, user-initiated)"


def valid_whisper_name(name: str) -> bool:
    """A model name, never a path: it becomes one directory below the models root."""
    return bool(_WHISPER_NAME.fullmatch(name)) and ".." not in name


def find_whisper_model(root: Path, name: str) -> Path | None:
    """The directory holding Whisper model `name` below `root`, or None when it is not installed.

    `<root>/<name>` is where `--download-whisper` puts it. Earlier releases let faster-whisper
    download into its Hugging Face cache layout below the same root
    (`models--<org>--faster-whisper-<name>/snapshots/<rev>`); such a model is used as it is,
    without fetching anything again. An absolute `name` pointing at a model directory is accepted
    as well.
    """
    explicit = Path(name)
    if explicit.is_absolute():
        return explicit if (explicit / WHISPER_MODEL_FILE).is_file() else None
    if not valid_whisper_name(name):
        return None
    direct = root / name
    if (direct / WHISPER_MODEL_FILE).is_file():
        return direct
    if not root.is_dir():
        return None
    for cache in sorted(root.glob(f"models--*--*whisper-{name}")):
        for snapshot in sorted((cache / "snapshots").glob("*")):
            if (snapshot / WHISPER_MODEL_FILE).is_file():
                return snapshot
    return None


def _faster_whisper_download(name: str, output_dir: Path) -> None:
    from faster_whisper import download_model

    download_model(name, output_dir=str(output_dir))


def download_whisper(
    root: Path,
    name: str,
    *,
    echo: Callable[[str], None] = print,  # CLI helper; the caller passes typer.echo
    force: bool = False,
    downloader: WhisperDownloader | None = None,
) -> int:
    """Fetch Whisper model `name` into `<root>/<name>`. User-initiated only (CLI).

    Downloads into `<name>.part` and renames on success, so an interrupted download never leaves
    a directory the engine would try to load. Returns a process exit code.
    """
    if not valid_whisper_name(name):
        echo(f"not a Whisper model name: {name!r}")
        return 2
    target = root / name
    if (target / WHISPER_MODEL_FILE).is_file() and not force:
        echo(f"exists   {target}")
        return 0
    fetch = downloader or _faster_whisper_download
    tmp = root / f"{name}.part"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    echo(f"fetching Whisper model {name!r}\n      -> {target}")
    try:
        fetch(name, tmp)
    except ImportError:
        shutil.rmtree(tmp, ignore_errors=True)
        echo("failed   faster-whisper is not installed - install the `voice` extra first")
        return 1
    except Exception as exc:  # noqa: BLE001 - a CLI download reports, it does not traceback
        shutil.rmtree(tmp, ignore_errors=True)
        echo(f"failed   {name}: {type(exc).__name__}: {exc}")
        return 1
    if not (tmp / WHISPER_MODEL_FILE).is_file():
        shutil.rmtree(tmp, ignore_errors=True)
        echo(f"failed   {name}: the download contains no {WHISPER_MODEL_FILE}")
        return 1
    shutil.rmtree(target, ignore_errors=True)
    tmp.replace(target)
    echo(f"done     Whisper model {name!r} is in {target}")
    return 0
