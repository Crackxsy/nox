"""`nox node voice setup`: the models the satellite loads, downloaded into the node's folder only
after a yes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nox.node.device.voice.settings import VoiceSettings, voice_config, voice_support


def _models_missing(exc: BaseException) -> bool:
    """Whether loading failed because a model is not downloaded yet (never fetched by itself)."""
    names = {type(e).__name__ for e in (exc, exc.__cause__, exc.__context__) if e is not None}
    hints = ("LocalEntryNotFound", "EntryNotFound", "FileNotFound", "missing")
    return any(hint in name for name in names for hint in hints) or "missing" in str(exc)


#: The Hugging Face revision of each faster-whisper model `nox node voice setup` fetches - a commit,
#: so the files are exactly the ones this release was checked with, whatever `main` becomes.
#: Read from the repositories' heads on 2026-11-12; moved on purpose, with a release.
WHISPER_REVISIONS = {
    "tiny": "d90ca5fe260221311c53c58e660288d3deb8d356",
    "base": "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66",
    "small": "536b0662742c02347bc0e980a01041f333bce120",
}


def _download_whisper(model: str, target: Path) -> None:
    """faster-whisper's own download, pinned to `WHISPER_REVISIONS`, into a plain folder
    `<target>/whisper-<model>` - which the speech engine prefers over its cache
    (`FasterWhisperStt._load_sync`)."""
    from faster_whisper.utils import download_model  # noqa: PLC0415 - the extra `voice`

    revision = WHISPER_REVISIONS.get(model)
    if revision is None:
        raise ValueError(f"no pinned revision for the model {model!r}")
    folder = target / f"whisper-{model}"
    folder.mkdir(parents=True, exist_ok=True)
    download_model(model, output_dir=str(folder), revision=revision)


def _download_tts(engine: str, target: Path, echo: Callable[[str], None]) -> int:
    from nox.voice.models import download_engine  # noqa: PLC0415

    return download_engine(engine, target, echo=echo)


def voice_setup(
    home: Path,
    settings: VoiceSettings,
    *,
    confirm: Callable[[str], bool] | None,
    echo: Callable[[str], None] = print,
    download_whisper: Callable[[str, Path], None] = _download_whisper,
    download_tts: Callable[[str, Path, Callable[[str], None]], int] = _download_tts,
) -> int:
    """`nox node voice setup`: the speech models for this device, into its own folder (or
    `voice.models_dir`) - only after a yes (`confirm`; None: the person said `--yes`). Speech
    recognition (Whisper, sized for this device) and the configured voice (Supertonic 3; Piper
    voices are placed by hand). 0 when everything is there, 1 when refused or failed."""
    from nox.node.device.cli_text import t  # noqa: PLC0415 - the words load only for this command
    from nox.voice.models import engine_models_dir  # noqa: PLC0415

    support = voice_support()
    if support is not None:
        echo(t("vs_not_installed", reason=support))
        return 1
    cfg = voice_config(settings, home)
    stt_model = str(cfg.stt.model)
    whisper_dir = engine_models_dir("faster-whisper", cfg.models_dir)
    engine = settings.tts_engine
    tts_dir = engine_models_dir(engine, cfg.models_dir)
    sizes = {"tiny": "75 MB", "base": "145 MB", "small": "480 MB"}
    size = sizes.get(stt_model, "?")
    question = t(
        "vs_question",
        model=stt_model,
        size=size,
        where=whisper_dir,
        voice=t("vs_voice_part") if engine == "supertonic" else "",
    )
    if confirm is not None and not confirm(question):
        echo(t("vs_nothing"))
        return 1
    try:
        echo(t("vs_whisper", model=stt_model, where=whisper_dir))
        download_whisper(stt_model, whisper_dir)
    except Exception as exc:  # noqa: BLE001 - reported, nothing half-claimed
        echo(t("vs_whisper_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}"))
        return 1
    if engine == "piper":
        echo(t("vs_piper", where=tts_dir))
        return 0
    code = download_tts(engine, tts_dir, echo)
    if code != 0:
        echo(t("vs_tts_failed", engine=engine, code=code))
        return 1
    echo(t("vs_done"))
    return 0
