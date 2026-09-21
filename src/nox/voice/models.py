"""Where voice model files live, and how the user gets the Kokoro ones.

Nox never downloads a model on its own. Engines resolve their files below a models root and, when a
file is missing, report `unavailable` with the exact path plus the command that fetches it - the
decision to pull ~354 MB over the network stays with the user (`nox voice download-kokoro`, i.e.
`python -m nox.worker --download-kokoro`).

Resolution order for the models root:
1. `voice.models_dir` from the configuration, when set; 2. `$NOX_DATA_DIR/models`, when the process
was started with that variable; 3. `%APPDATA%/Nox/models`, which mirrors the `paths.data_dir`
default in `config/defaults.yaml`. A deployment that moved `paths.data_dir` elsewhere sets
`voice.models_dir` explicitly - the voice worker only ever receives the `voice` section of the
configuration, never `paths`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

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
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Nox"


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
        if target.is_file() and target.stat().st_size > 0 and not force:
            echo(f"exists   {target}")
            continue
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
        tmp.replace(target)
        echo(f"done     {name} ({done / 1e6:.1f} MB)")
    echo(f"Kokoro model files are in {target_dir}. Set `voice.tts.engine: kokoro` to use them.")
    return 0
