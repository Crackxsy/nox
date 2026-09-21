"""Download the voice models used by Nox into a local, git-ignored models directory (see ADR-009).

Piper voices come from Hugging Face rhasspy/piper-voices via httpx; faster-whisper models are
fetched through the library's own downloader (huggingface_hub) with download_root set here.
Usage: .venv/Scripts/python.exe spikes/download_models.py [--whisper base,small]
Set NOX_SPIKE_MODELS_DIR to use a different directory than the default (`spikes/out/models/`,
already covered by `.gitignore`).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

MODELS_DIR = Path(
    os.environ.get("NOX_SPIKE_MODELS_DIR", Path(__file__).resolve().parent / "out" / "models")
)
PIPER_DIR = MODELS_DIR / "piper"
WHISPER_DIR = MODELS_DIR / "faster-whisper"

HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
PIPER_VOICES = {
    "de_DE-thorsten-medium": f"{HF}/de/de_DE/thorsten/medium/de_DE-thorsten-medium",
    "en_US-lessac-medium": f"{HF}/en/en_US/lessac/medium/en_US-lessac-medium",
}


def download(url: str, target: Path) -> None:
    if target.exists() and target.stat().st_size > 0:
        print(f"exists   {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", "0"))
        done = 0
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
        print(f"fetched  {target} ({done / 1e6:.1f} MB of {total / 1e6:.1f} MB)")
    tmp.replace(target)


def download_piper() -> None:
    for name, base in PIPER_VOICES.items():
        download(f"{base}.onnx", PIPER_DIR / f"{name}.onnx")
        download(f"{base}.onnx.json", PIPER_DIR / f"{name}.onnx.json")


def download_whisper(sizes: list[str]) -> None:
    from faster_whisper import WhisperModel  # noqa: PLC0415

    WHISPER_DIR.mkdir(parents=True, exist_ok=True)
    for size in sizes:
        print(f"loading  faster-whisper {size} -> {WHISPER_DIR}")
        WhisperModel(size, device="cpu", compute_type="int8", download_root=str(WHISPER_DIR))
        print(f"ready    faster-whisper {size}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper", default="base,small")
    ap.add_argument("--no-piper", action="store_true")
    args = ap.parse_args()
    if not args.no_piper:
        download_piper()
    sizes = [s for s in args.whisper.split(",") if s]
    if sizes:
        download_whisper(sizes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
