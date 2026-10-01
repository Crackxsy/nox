# Local models

Nox talks to Ollama for its local model. Which one you pick matters more than it looks, and not for
the reason people expect.

## The thing that decides it

Nox reaches its tools through a text protocol: the model answers with one `NOX_TOOL_CALL {...}` line
and nothing else, and the core parses it. A model that writes a friendly paragraph instead can talk
and do nothing - no lights, no files, no windows. Speed is the second question.

`scripts/probe_models.py` asks every model Ollama has the real question, with the real offer text
and the real parser, and reports both:

```powershell
.venv\Scripts\python scripts\probe_models.py
```

Four cases: three that need a specific tool, and one that needs the model *not* to reach for one -
which is the half most small models fail.

## Measured, on a GTX 1060 6 GB / Ryzen 7 7800X3D / 32 GB

| Model | Protocol | Seconds per answer | Notes |
| --- | --- | --- | --- |
| `qwen3:8b` | 4/4 | 32.7 | Correct and unusable. A thinking model: the reasoning costs the seconds |
| `gemma4:e2b` | 4/4 | 8.7 - 17.3 | Mixture-of-experts: 7.2 GB on disk, 1.7 GB resident, 100% on the GPU |
| `qwen3:4b` | 4/4 | 14.1 | The thinking variant of the one below |
| `qwen3:4b-instruct` | 3/4 | **2.9 - 3.0** | 3.2 GB resident, 100% on the GPU. **The default** |
| `mistral:latest` | 3/4 | 5.0 | Reached for a tool when it should have chatted |
| `llama3.2:3b` | 1/4 | 14.6 | **Was the default until 0.3.0.** Writes prose where a directive belongs |
| `llama3.2:1b` | 1/4 | 1.1 | Fast and useless for this |

Your numbers will differ; the ranking is the point. Run the probe rather than trusting the table.
The spread on `gemma4:e2b` is what measurement noise looks like when several models are still
resident and competing for the card - probe one model at a time if the number matters to you.

Two findings worth more than the table:

**The old default could not drive its own tools.** `llama3.2:3b` hit 1 of 4. Nox shipped pointing at
a model that answers a request for a tool with a friendly paragraph, so the local fallback could
talk and do nothing. That is fixed in 0.3.0.

**Bigger is not the win here.** An 8B model fits this 6 GB card and answers in 32.7 seconds, because
the current crop of larger open models reason before they answer and the thinking is most of the
time. The 4B *instruct* variant of the same family is ten times faster and only one case worse. Pick
an instruct model that fits, not the largest one that loads.

## What fits in how much VRAM

Rules of thumb for a Q4 quantisation, which is what Ollama pulls by default:

| Model size | Roughly | 6 GB card | 8 GB | 12 GB+ |
| --- | --- | --- | --- | --- |
| 1-4B | 1-3 GB | comfortable | comfortable | comfortable |
| 7-8B | 4.5-5.5 GB | fits, little context left | comfortable | comfortable |
| 12-14B | 8-9 GB | no - part runs on the CPU | tight | comfortable |
| 27-32B | 16-20 GB | no | no | needs 24 GB |

"Does not fit" is not a failure. Ollama puts as many layers on the GPU as it can and runs the rest
on the CPU, which is why `gemma4:e2b` above answers in 8.7 s rather than not at all. On a 7800X3D
that is survivable. It is still three times slower than a model that fits.

## About running a model bigger than your memory

There are projects that stream a model's layers from an SSD so that a 70B model "runs" on a laptop.
They work, and the arithmetic is unkind: every token needs every layer, so every token reads tens of
gigabytes from disk. Expect seconds to minutes *per token*.

That is a reasonable trade for a batch job you start before bed. It is not one for an assistant that
is supposed to answer while you are still looking at it - and Nox speaks its answers sentence by
sentence, so a slow first token is a silence you sit through.

The order of preference is therefore:

1. A model that fits in VRAM.
2. A model that fits in VRAM + RAM, with the CPU carrying some layers. Ollama does this for you.
3. Not a model that needs the disk.

A smaller model that fits beats a larger one that swaps, every time.

## Changing the model

In your `%APPDATA%\Nox\user.yaml`:

```yaml
ai:
  providers:
    ollama:
      model: "qwen3:4b-instruct"
```

Then `.venv\Scripts\nox doctor` to confirm Ollama has it, and the probe to confirm it can drive the
tools. A model Nox cannot reach says so in `doctor` rather than failing later.

## If you have a stronger card

Nothing here assumes 6 GB. With 12 GB or more, a 12-14B instruct model is the sweet spot for this
protocol: large enough to follow a format reliably, small enough to answer in a second or two. Pull
it, run the probe, and keep whichever one wins the protocol column - that is the number that decides
whether Nox can act or only talk.
