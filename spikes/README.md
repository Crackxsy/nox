# Spikes

Throwaway feasibility experiments, kept for the record of what was actually measured before a
design decision was made. They are **not** part of the product and are **not** held to
`docs/CODE_STANDARDS.md` - no strict typing, no test coverage, no long-term API stability. Expect
hard-coded constants, print-based output and scripts that assume they are run once, by hand, from
a checkout.

Run any of them with the project venv, e.g.:

```powershell
.venv\Scripts\python.exe spikes\sp01_claude_code.py --runs 5
```

## What's here

| File | Measures |
| --- | --- |
| `sp01_claude_code.py` | Claude Code CLI as a chat provider: latency, streaming, logged-out behaviour. |
| `sp02_stt.py` | faster-whisper STT latency, base vs small model, CPU int8. |
| `sp03_tts.py` | Piper vs Kokoro TTS: load time, time-to-first-audio, real-time factor. |
| `sp07b_shell_rss.py` | Desktop shell process-tree memory (RSS) with GPU vs software compositing. |
| `sp09_qt_transparent.py` | Frameless transparent always-on-top `QWebEngineView` on Windows 11. |
| `sp12_ollama.py` | Ollama local models: time-to-first-token, tokens/s, GPU vs CPU, embedding latency. |
| `download_models.py` | One-off helper: fetches the voice models the STT/TTS spikes need into a local, git-ignored directory (override with `NOX_SPIKE_MODELS_DIR`). |

## Output policy

- `spikes/results/*.json` and small text summaries (e.g. `sp09_run.txt`) are **tracked** - they
  are the actual measurements a decision was based on, small enough to keep in the repository.
- `spikes/out/` (raw logs, WAV files, screenshots, anything larger or reproducible on demand) is
  **git-ignored**. Re-run the relevant spike to regenerate it.

## Before adding a new spike

Strip anything a stranger reading this repository could not act on: no personal names, machine
paths, or references to documents that live outside this repository. If a measurement changes a
real decision, record the outcome in `CHANGELOG.md` or an ADR, not only in this directory.
