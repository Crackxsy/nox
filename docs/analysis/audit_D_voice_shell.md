> **Provenance.** Machine-assisted subsystem audit of the tree between `6625b50` and `3af0a5a` (read-only, code-reading
> with a few scratch experiments), written for [`../CAPABILITY_ANALYSIS.md`](../CAPABILITY_ANALYSIS.md).
> Line numbers may be off by a few lines for files changed since. Findings that the consolidated analysis relies on were re-checked
> by hand and are marked there; treat everything else here as a well-sourced lead, not a verdict.

# Audit D: voice, sensing, desktop shell

Scope: `src/nox/voice/`, `src/nox/worker/main.py` (voice path) and `heartbeat.py`, `src/nox/sensors/`, privacy-zone detection (`src/nox/security/privacy.py`), `src/nox/shell/`, the tests under `tests/unit/{voice,sensors,shell}`, `tests/integration/test_sensors.py` and `test_clips.py`, and `spikes/` (sp02, sp03, sp09). The working tree includes the uncommitted cross-platform changes. The audit was read-only. Two scratch scripts, run against the test fakes with `.venv/bin/python` (outside the repo), confirmed findings R3, R4, R8, R11 and the first case of R1. Line numbers refer to the current working tree.

Default configuration (`config/defaults.yaml:87-128`):
- `listening_mode: ptt_only`
- `wake_word_engine: openwakeword`, which falls back to `text` because no model exists
- Whisper `small`, CPU int8
- Piper `de_DE-thorsten-medium`
- `private_device: ""` (system default output)
- `barge_in: true`

---

## 1. Capability inventory

| Capability | What the user can actually do | Entry points | Key files | Maturity | Tests |
|---|---|---|---|---|---|
| Push-to-talk voice input (default mode) | Hold Ctrl+Alt+Space. The microphone opens, the speech is transcribed and sent to the orchestrator as addressed. | pynput hotkey → shell `voice.ptt` → core `voice_ptt` (`ipc/handlers/core.py:325-332`, 2 s timeout) → worker `voice.ptt` (`worker/main.py:243-245`) → `pipeline.push_to_talk` (`voice/pipeline.py:361-385`) | pipeline.py, audio.py, vad.py, shell/hotkeys.py | WORKS-UNPROVEN-IN-REAL-ENV. Only fakes run in CI, and CI does not install the `voice` extra (`.github/workflows/ci.yml:35,95`). | test_pipeline_gate.py:264-318, test_capture_gate_hardware.py (fakes) |
| `ptt_only` keeps the device closed | The microphone and the OS indicator are off until PTT is pressed. | `refresh_gate` → `SoundDeviceInput.set_enabled` (audio.py:353-367) | pipeline.py:160-179 | WORKS+TESTED as a property of the fakes. The real device path is unproven. | test_capture_gate_hardware.py:34-100 |
| Continuous listening with the text wake word | Say "Nox, …". Every VAD segment is transcribed and the wake word is matched on the text. | `listening_mode: continuous` | wake_gate.py:74-97 (`TextFallbackDetector`), wake_word.py | LIMITED: backlog (R9), echo (R5), hallucinations (R10) | test_pipeline.py:86-129, test_wake_word.py |
| Acoustic wake word (openWakeWord) | Nothing, unless the user trains and supplies their own `.onnx` model. | `<models_dir>/openwakeword/*.onnx` | wake_gate.py:111-192 | SCAFFOLDING: no "Nox" model exists, and the conversation window is broken (R4) | test_wake_gate.py (fake detector) |
| Conversation window (follow-ups without the wake word) | Nothing works. Follow-ups are transcribed but emitted as unaddressed and dropped. | — | pipeline.py:344, orchestrator.py:191 | BROKEN (R4) | test_pipeline_gate.py:174 only counts events |
| STT (faster-whisper) | German/English transcription on the CPU. A second language pass runs when detection falls outside {de, en}. | pipeline, and the `stt.transcribe` IPC request (worker/main.py:233-241) | stt/faster_whisper_engine.py | WORKS-UNPROVEN-IN-REAL-ENV. No unit test touches the real engine; only a hardware-marked roundtrip exists (test_hardware.py:47-61). | test_worker.py:252 (fake STT) |
| TTS Piper (German) | Spoken German replies, streamed per sentence. | core `WorkerSpeaker.say` → `tts.speak` (core/boot/workers.py:52-63) | tts/piper_engine.py, tts/sentences.py | WORKS-UNPROVEN-IN-REAL-ENV. `PiperTts` synthesis is not unit-tested. | test_sentences.py, test_hardware.py |
| TTS Kokoro | English voices. German text is read by the English `af_heart` voice. | `voice.tts.engine: kokoro`, `--download-kokoro` | tts/kokoro_engine.py, voice/models.py | LIMITED, and health says so honestly (kokoro_engine.py:117-127) | test_kokoro_tts.py (20, faked) |
| Pre-rendered clips (RL callouts) | Play `<clips>/<id>.wav` without synthesis. | `TtsRequest.prepared_clip` | tts/clips.py | WORKS+TESTED | test_pipeline.py:312, integration test_clips.py |
| Barge-in | Speaking or pressing PTT while Nox talks stops the current sentence only. | VAD START / PTT press → `interrupt` | pipeline.py:277-281,375-376,454-458; audio.py:296-305 | LIMITED (R3) | test_pipeline.py:227-256 (single utterance only) |
| Kill phrase "Nox Notaus" | Engages the kill switch locally. It never goes through the LLM. | transcript → `VOICE_KILL_PHRASE` → app.py:761-765 | wake_word.py:22-29,73-81; pipeline.py:334-343; worker/main.py:159-167 | LIMITED (R11, R12). Needs PTT held in the default mode. | test_wake_word.py:62-74, test_pipeline.py:130, test_pipeline_gate.py:222-318 |
| Mute | Tray, hotkey or dashboard → `voice.mute`. | handlers/core.py:334-346 | pipeline.py:387-392 | WORKS, but mute is lost when the worker restarts (R1) | test_pipeline.py:178, test_worker.py:119 |
| Privacy gating of the mic | Mode, capture flag, zone, panic and kill switch all close the capture device. | `privacy.capture_changed` → worker (worker/main.py:181,267-270) | security/privacy.py:261-267 | LIMITED: no initial sync (R1), zone globs are broad (R13) | test_pipeline.py:192, test_worker.py:146-200 |
| Output routing private/stream/both/mute | Speech can go to a virtual cable for the stream. | `voice.channels.routing` → orchestrator channel (app.py:432) | audio.py:224-239 | WORKS-UNPROVEN-IN-REAL-ENV; the named-device sample rate is a risk (R7) | none with real devices |
| Selftest | `python -m nox.worker --selftest` / `nox voice selftest`: devices, model load, TTS, 3 s microphone. | CLI | worker/main.py:604-705 | WORKS-UNPROVEN-IN-REAL-ENV. Windows-only device list, default config only (R25). | test_worker.py:314-329 (args only) |
| Kokoro download | `--download-kokoro` fetches ~354 MB. | CLI | voice/models.py:71-107 | WORKS-UNPROVEN-IN-REAL-ENV, with no integrity check (R20) | test_models_paths.py |
| Worker reconnect and re-register | The worker re-registers after the hub drops it. | `on_connection_change` (worker/main.py:332-355) | worker/main.py | WORKS+TESTED | test_worker_reregister.py, test_worker_connect_retry.py |
| Worker crash recovery | None. A dead voice worker stays dead until the core restarts. | — | core/boot/workers.py:146-160; app.py:624-627 | MISSING (R2) | — |
| Foreground and privacy-zone sensor | Zones close capture, screen and memory. Unobservable desktops fail closed. | 1 s poll | sensors/foreground.py, probe.py, win32.py, posix.py | Windows: WORKS-UNPROVEN-IN-REAL-ENV (`RealWin32Probe` has no test). X11/mac parsers: WORKS+TESTED with a fake runner. Wayland: fail-closed. | test_foreground.py (7), test_probe.py (23), integration test_sensors.py (3) |
| Idle/away sensor | Sets `user.present` and `user.activity` idle/away at 10/20 min. | 5 s poll | sensors/idle.py, win32.py:51-63 | Windows: BROKEN between 24.9 and 49.7 days of uptime (R14). Other platforms: WORKS+TESTED with fakes. | test_idle.py (4) |
| Resources (CPU/RAM/GPU) | CPU and RAM always; GPU/VRAM only with `nvidia-smi`. | adaptive poll | sensors/resources.py | CPU/RAM: WORKS+TESTED. GPU: LIMITED (NVIDIA only; 0.0 is written when unknown). | test_resources.py (4) |
| Game process hook | `sensor.process_started` / `sensor.process_ended` for the configured executables. | 5 s poll | sensors/game.py | WORKS+TESTED (fake lister). The poll blocks the event loop (R24). | test_game.py (3) |
| `sensors.status.read` tool | Latest sample per sensor, for the LLM and dashboard. | tool registry | sensors/tools.py, history.py | WORKS+TESTED | test_tools.py, test_history.py |
| Pet window | Frameless, transparent, always-on-top, draggable, click-through. | shell | shell/pet_window.py, win32.py | WORKS on Win11 (sp09: 60.1 fps, 16.8 ms max frame gap, 280.7 MB RSS tree, click-through verified). Multi-monitor is unguarded (R18). | test_pet_window.py (3, offscreen), test_win32.py |
| Tray | Tint by state; privacy, mute, dashboard, kill, quit. | Qt | shell/tray.py, logic.py | WORKS+TESTED, but the state can lie after reconnect (R15) | test_app.py (14), test_logic.py (12) |
| Global hotkeys | PTT, mute, privacy, kill, show/hide pet. | pynput listener thread | shell/hotkeys.py, logic.py:170-207 | LIMITED: user config is ignored (R16); stuck keys and elevated windows break it (R17); nothing works on Wayland (R12) | test_hotkeys.py (6, pure tracker) |
| Permission dialog | Native allow/deny with a session memory. | `security.permission_requested` | shell/dialogs.py | WORKS+TESTED | test_dialogs.py, test_app.py:117 |
| Shell↔core reconnect | Detects a core restart, re-reads the token and endpoints, reloads the pet page. | ping every 5 s; drop after 2 failures; retry every 3 s | shell/app.py:195-293 | WORKS+TESTED, but model state is not resynced (R15) | test_app_reconnect.py (4) |
| Kill via supervisor when the core is down | Tray or hotkey → `sup.kill` over the control port. | shell/supervisor_client.py | shell/app.py:410-437 | WORKS+TESTED | test_supervisor_client.py, test_app.py:150-197 |
| AEC, speaker verification, streaming/partial STT, custom wake model, input-device selection | — | — | `Transcript.partial` exists (base.py:32) but is never set; there is no input-device config key | MISSING | — |

---

## 2. Voice pipeline walkthrough

**Process and threads.** Everything runs in one `voice` worker process: `python -m nox.worker --service voice`, spawned by `WorkerSupervisor.spawn` (core/boot/workers.py:115-134). Its token comes from the environment. On Windows the worker runs at ABOVE_NORMAL priority (worker/main.py:400-409). The worker has one asyncio loop plus these threads:
- **PortAudio input callback thread.** `SoundDeviceInput._callback` (audio.py:400-414) resamples to 16 kHz inside the callback and calls `call_soon_threadsafe` once per 30 ms frame. The loop-side `_enqueue` caps the queue at 200 frames by dropping the oldest (audio.py:416-425).
- **Loop, `_listen` task** (pipeline.py:226-257). Checks the gate, then `EnergyVad` and `Segmenter` in numpy (vad.py), and puts every frame on `_detect_queue`, which is unbounded (pipeline.py:112,245).
- **Loop, `_detect_loop`** (pipeline.py:259-275). Batches frames and runs `WakeGate.feed_many` through `asyncio.to_thread` on the default executor. With the text fallback, `feed_many` returns immediately (wake_gate.py:265-274).
- **Loop, `_transcribe_loop`** (pipeline.py:292-303). The single consumer of `_pending`, which is unbounded (pipeline.py:113). Runs `FasterWhisperStt.transcribe` through `to_thread`, with ctranslate2 `cpu_threads = min(8, cpu/2)` (faster_whisper_engine.py:52).
- **TTS.** One dedicated `threading.Thread` per request for Piper or Kokoro (piper_engine.py:140-155, kokoro_engine.py:208-222), feeding an unbounded `asyncio.Queue`. `SoundDeviceOutput.play` feeds a `bytearray` per `_DevicePlayer`, and the PortAudio output callback thread drains it (audio.py:131-149). Device open, drain, abort and close go through `to_thread`.
- Heartbeat task and IPC client.

**Capture.** Frames are 16 kHz mono float32, 30 ms (480 samples).
- The input device is always the system default. `build_components` passes no device (worker/main.py:467), and `VoiceChannelsConfig` has no input field (assistant.py:77-80).
- Opening tries 16 000, then 48 000, then 44 100 Hz (audio.py:376). The fallback resampler is linear interpolation with no low-pass filter (audio.py:91-98), so aliasing is possible (R7).
- In `ptt_only` the stream opens at PTT press and closes at release (pipeline.py:369-371,385).

**VAD and segmenter** (vad.py):
- A frame counts as speech when RMS > -50 dBFS, is at least 8 dB above an adaptive floor (the floor is capped at -25 dBFS), and the zero-crossing rate is between 0.005 and 0.45.
- A segment starts after 3 speech frames (90 ms), with 300 ms of pre-roll.
- It ends after 600 ms of silence, or at the 15 s maximum.
- Segments shorter than 250 ms are aborted, including PTT taps.
- PTT forces a segment open (`force_start`/`force_end`).
- `end_silence_ms`, `max_utterance_ms`, `min_utterance_ms` and `require_wake_word` are not wired from configuration (worker/main.py:427-433).

**Wake gate** (wake_gate.py:301-313). Decision order: PTT → TEXT_FALLBACK (no acoustic detector, which is the default reality) → WAKE (fired within 8 s) → CONVERSATION (addressed within 20 s) → KILL_WATCHDOG (segment ≤ 2.5 s) → DROP.

**STT** (faster_whisper_engine.py:88-137):
- `beam_size=1`, `vad_filter=False`, `condition_on_previous_text=False`, `without_timestamps=True`.
- With `auto`, a detected language outside {de, en} triggers a full second pass with the best allowed language (line 111), which doubles latency.
- Confidence is the mean of `exp(avg_logprob)`. It is reported but never used for gating.

**Matching** (wake_word.py):
- Normalise the text, skip up to 2 greetings, then fuzzy-match the first token (difflib ≥ 0.75 or a spelling list).
- The kill phrase counts anywhere in the utterance as (wake token + "notaus" / "not aus" / "nothalt" / "not halt" / "emergency stop" / "emergency shutdown").
- A kill latches the pipeline, emits `voice.kill_phrase`, and the worker enters safe mode (worker/main.py:159-163). The core then engages the kill switch (app.py:761-765), which terminates the workers. Resume respawns them (`ensure_voice_worker`, app.py:624-627).

**Orchestrator.** The worker emits `voice.transcript_ready`. `Orchestrator._on_transcript` drops anything with `addressed_to_nox=False` (orchestrator.py:190-191). Otherwise it starts a voice turn as a task and streams from the LLM router. Each finished sentence becomes one `tts.speak` request (orchestrator.py:286-296, 371-380). The TTS `language` is the Whisper-detected language of the user's utterance. `tts.speak` is acknowledged immediately; the worker serialises utterances on `_say_lock` (pipeline.py:397).

**TTS and playback.**
- Sentence split: sentences.py.
- Piper yields chunks as synthesized. Kokoro yields one chunk per sentence.
- `SoundDeviceOutput.play` opens a new PortAudio `OutputStream` for every utterance (every sentence) at the engine rate (22 050 or 24 000 Hz) with no rate fallback (audio.py:118-129, 266-270). It then feeds the stream, marks EOF, waits for drain (timeout 30 s), and closes.

**Barge-in.**
- VAD START while speaking, or a PTT press, calls `interrupt`, which aborts the active player: the buffer is cleared and the stream aborted (audio.py:163-171).
- The stop latency is measured into `last_stop_latency_ms` (audio.py:303) and only logged.
- A hardware-marked test asserts < 200 ms (test_hardware.py:26-44).
- Only the current sentence is stopped (R3).

**Latencies measured anywhere:**
- Piper: ~90 ms to first audio, RTF 0.03. Kokoro: 750-840 ms to first audio, RTF 0.22. Both for 7 s of speech on CPU (README.md:230, from sp03). These exclude the output device open.
- Whisper: only the comment "8 physical-core threads 35 % faster than default" (faster_whisper_engine.py:50-51). sp02 results are in the git-ignored `spikes/out/`. No STT latency number is committed.
- Barge-in stop: the < 200 ms assertion in the opt-in hardware test.
- Shell: sp09_run.txt (60.1 fps, 280.7 MB, 0.1 % CPU idle).
- There is no end-to-end number anywhere (speech end → first audio). The structural floor is 600 ms end-silence + Whisper `small` on CPU (not measured in the repo) + a possible second language pass + LLM time to first sentence + Piper ~90 ms + device open. `TranscriptReady.latency_ms` and orchestrator `TurnTimings` exist, but nothing aggregates them.

---

## 3. Reliability findings

Severity: **H** = user-visible failure or privacy/safety impact in a realistic setup · **M** = degraded or confusing behaviour · **L** = minor or edge case. "Verified" means reproduced with a scratch script against the repo's test fakes.

**R1 (H) The worker never learns the current privacy, mute or kill state when it (re)registers.**
- The worker starts with `WorkerStatus.microphone_allowed=True` (worker/main.py:80) and `_muted=False` (pipeline.py:101).
- `worker.register` returns only `{"ok", "config"}` (ipc/handlers/core.py:403).
- The capture state reaches the worker only through `privacy.capture_changed` (worker/main.py:181,267-270), which is published only on a *change* (security/privacy.py:342,384,409,423).

Scenarios:
- (a) `privacy.capture.microphone: false` in user.yaml with `listening_mode: continuous`: the worker opens the mic at start anyway.
- (b) The user muted Nox, then the worker reconnects or restarts. It is unmuted, while core state says muted. The comment at handlers/core.py:345 ("the worker picks the flag up when it reconnects") is not implemented anywhere.
- (c) On Wayland or macOS, the `unobservable` zone is entered on the first foreground poll. If that happens before the worker subscribes, the worker never sees the closed gate, so the result depends on boot order.

Verified: a fresh worker reports `microphone_allowed True, muted False` with no privacy information from the core.

**R2 (H) A voice worker crash or engine-load failure is never recovered and is poorly reported.**
- A disconnect only calls `detach` (core/boot/workers.py:146-160). The `_workers` entry stays, so `ensure_voice_worker` does nothing (app.py:624-627). There is no respawn loop.
- Any `load()` failure propagates out of `VoiceWorker.run` (worker/main.py:320-321), and `run_worker` catches only KeyboardInterrupt/CancelledError (596-601), so the process exits 1. Examples: a missing Piper voice raises FileNotFoundError (piper_engine.py:98-99); Whisper download fails offline; Kokoro gets an unknown voice id because `voice.tts.voice` still holds a Piper id after an engine switch (kokoro_engine.py:152-159 via worker/main.py:487).
- Core health then says only "worker not running" (core/boot/health.py:58-70). README.md:219 promises "a missing model is an `unavailable` health reason that names the path"; that is true inside the worker only.
- A crash mid-utterance leaves no `tts.finished`. `PetService` stays "speaking" (pet/service.py:175-182, no reset on disconnect), and `WorkerSpeaker.say` silently logs `speaker.no_voice_worker` (core/boot/workers.py:59) for every later reply.

**R3 (H) Barge-in only cuts the current sentence; the rest of the queued reply keeps playing.**
- Each sentence is its own `tts.speak`, so its own `say` task queued on `_say_lock` (pipeline.py:397).
- `interrupt` acts only on the utterance that is `_speaking` (454-458).
- The next queued `say` resets `_interrupt_reason=None` (400), and `SoundDeviceOutput.play` captures a *fresh* stop generation (audio.py:251). The CHANGELOG fix "a tts.stop that arrives while an utterance is still queued now cancels that utterance" is therefore defeated by the pipeline lock that sits in front of it.
- `Orchestrator.cancel` (orchestrator.py:175-187) stops the LLM stream, but sentences already accepted keep playing.

Verified: of 4 queued sentences, s0 stopped after 3 of 10 chunks, and s1, s2 and s3 played in full with `tts.finished ok`.

**R4 (H, acoustic mode) Conversation-window follow-ups are emitted as unaddressed, so they are dropped.**
- `addressed = forced or match.addressed or decision is GateDecision.WAKE` (pipeline.py:344) leaves out CONVERSATION, and the orchestrator drops unaddressed transcripts (orchestrator.py:191).
- The feature costs a Whisper pass and does nothing.
- test_pipeline_gate.py:174-201 counts events but never checks `addressed_to_nox`.

Verified: addressed flags were `[True, False]`.

**R5 (H in continuous mode) Echo and self-triggering with no AEC.**
- Capture is not half-duplex. `_listen` never looks at `_speaking`, and VAD START while speaking triggers barge-in (pipeline.py:279-281).
- With speakers (the default `private_device: ""` is the system output), Nox's own voice passes the 90 ms start threshold and interrupts nearly every sentence. Per R3, the next sentence then starts and is cut again, so speech becomes choppy.
- Text fallback: the echo is also transcribed. It is dropped as unaddressed, but it costs CPU and adds to the backlog.
- Acoustic mode: TTS saying "Nox" can fire the detector. The 8 s wake window then marks Nox's *own* next sentence as addressed, which starts a turn and cancels the current one, so it can feed back on itself.
- Kill phrase: if an answer contains "Nox, Notaus" (for example to "wie stoppe ich dich?"), the kill switch fires, because the kill check matches anywhere in the transcript (wake_word.py:73-81).
- `ptt_only` avoids all of this unless PTT is held while Nox speaks.

**R6 (M-H) Capture can die silently, and hot-plug and default-device changes are not handled.**
- When a device disappears, the PortAudio callback simply stops. `frames()` then waits forever (audio.py:449-456). Nothing watches `stream.active` or frame timestamps, and `capture_error` is only set when the iterator ends (pipeline.py:255-257).
- `capture_health()`, `wake_gate_health()`, `dropped_frames`, `underruns` and `last_stop_latency_ms` are never sent to the core. The heartbeat carries only `status` and `load` (worker/heartbeat.py:41-48). CHANGELOG.md:331-333 claims capture failure "is reported".
- The resolved output-device cache (audio.py:212-222) is never invalidated. `forget_devices()` has no caller, so after a hot-plug a named device's cached index can point at the wrong device.
- There is no input-device selection at all.

**R7 (M, unproven) Sample-rate mismatches on named devices.**
- Output opens at 22 050 or 24 000 Hz with no fallback (audio.py:118-129).
- `resolve_device` prefers WASAPI for named devices (audio.py:30,83). WASAPI shared mode usually rejects rates other than the mix format unless auto-convert is requested, and sounddevice does not request it by default.
- So configuring `private_device` or `stream_device` (for example VB-Cable) likely produces a PortAudioError and silent failure. The default device (None) goes through the PortAudio default host API (MME on Windows), which resamples.
- Input on a WASAPI named device likely falls back to 48 kHz and is decimated in the callback with no anti-alias filter (audio.py:406-407, 91-98), which folds 8-24 kHz energy into the speech band and hurts VAD zero-crossing rate and Whisper.

**R8 (L) Duplicate `tts.finished` on an unexpected playback error.**
- `say` emits `_finish(ok=False)` and then re-raises (pipeline.py:423-433). `_say_task_done` emits a second one (worker/main.py:204-221). This contradicts "exactly one terminal event" (CHANGELOG, test_capture_gate_hardware.py:106).

Verified: two identical `tts.finished` events.

**R9 (M) Unbounded queues and backlog in continuous text-fallback mode**, which is the only continuous mode available without a custom model.
- `_pending` is unbounded (pipeline.py:113), with one Whisper `small` consumer.
- Every VAD segment up to 15 s is transcribed, so with game or video audio the 20-40 s backlog described in wake_gate.py:3-7 returns.
- The kill-phrase latency then equals the backlog depth.
- `_detect_queue` (pipeline.py:112) is unbounded in acoustic mode if inference falls behind real time.
- A language misdetection doubles Whisper's cost (faster_whisper_engine.py:108-111).
- The worker's IPC `stt.transcribe` runs concurrently with the pipeline, so two Whisper passes can run at once at 8 threads each.

**R10 (M) Whisper hallucinations on silence or noise.**
- `vad_filter=False` (faster_whisper_engine.py:97). There is no blacklist for well-known German hallucinations ("Vielen Dank.", "Untertitel im Auftrag des ZDF", "Tschüss").
- Confidence is computed but never used for gating.
- A PTT press with ≥ 250 ms of silence is forced, therefore addressed, so the LLM answers a phantom "Vielen Dank."
- The only protection is faster-whisper's default skip (no_speech_prob > 0.6 AND avg_logprob < -1.0).

**R11 (M) "Nox Notaus" false negatives and false positives.** Verified against `WakeWordMatcher`:
- False negatives:
  - "Nox Nottaus", "Nox Notauss", "Nox, Nota aus", "Nuks Notaus".
  - "Nox bitte Notaus", "Nox... ähm Notaus": filler words break the adjacency requirement.
  - "Nox, not house" / "Knox not out": Whisper choosing English on a ~1 s clip with `language=auto`.
- False positives: "Nox, Notaus-Knopf erklären" and "Sag einfach Nox Notaus" both engage the kill switch.
- In the default `ptt_only` mode, the phrase works only while PTT is held. USER_GUIDE.md:68 does not say so.
- While a privacy zone is focused, the mic is off, so the phrase is unreachable. The hotkey still works.
- In acoustic mode, a kill phrase inside a segment > 2.5 s with no wake word is dropped. This is documented (wake_gate.py:26-33).

**R12 (H, cross-platform) Voice is unreachable on Wayland, and effectively on macOS without permissions, contrary to docs/PLATFORMS.md.**
- `voice.ptt` is allowed only for the `shell` role (ipc/dispatch.py:83); the dashboard cannot hold PTT.
- The shell's pynput listener has no Wayland or permission check (shell/hotkeys.py:101-111). Only the supervisor checks `hotkey_blocked_reason`. So with the default `ptt_only` there is no way to open the mic, and the user is not told.
- Separately, the permanent `unobservable` zone makes `allows_capture("microphone")` False (security/privacy.py:261-267), which is racy per R1.
- PLATFORMS.md:24 lists "Nox, Notaus" as a Wayland kill path, and PLATFORMS.md:34-45 says fail-closed turns off "screen capture, screenshots, clipboard reads and memory writes". It does not mention that the microphone, and with it all voice, is also off.

**R13 (M) Privacy-zone title globs are broad and silently disable the mic.**
- Built-in patterns such as `*bank*` match "Datenbank", `*signal*` matches "signal_handler.py", and `*depot*`, `*finanzen*`, `*password*` and `*messenger*` are similarly wide. Discord is zoned by default (security/privacy.py:55-133).
- Result: pressing PTT in VS Code on `datenbank.py` does nothing, with no feedback.
  - The pipeline only logs `voice.ptt_refused` (pipeline.py:363-365).
  - The core `voice_ptt` returns `ok: True` (handlers/core.py:325-332).
  - The shell's `ptt()` sends no `failure_text` (shell/app.py:386-387).
- UWP apps hosted by `ApplicationFrameHost.exe` never match process patterns; only titles can match them.

**R14 (M) Windows idle detection is broken from 24.9 to 49.7 days of uptime.**
- `kernel32.GetTickCount` keeps ctypes' default `c_int` return type, so it is signed (sensors/win32.py:60). `dwTime` is `c_uint` (39).
- After 2^31 ms the tick goes negative, `millis < 0`, and the function returns `0.0` (61-63). The user is never idle or away.
- The comment attributes the negative case to the 49.7-day wrap, which is wrong.
- Windows 11 Fast Startup keeps uptime across shutdowns, so this range is common.
- Fix: `GetTickCount64`, or `restype=c_uint32` plus `(tick - dwTime) & 0xFFFFFFFF`.
- `RealWin32Probe` has no test.

**R15 (M) The tray and capture indicator can lie.**
- `ShellModel` starts from defaults: BALANCED, unmuted, no capture, RUNNING (shell/logic.py:80-90).
- On connect the shell only subscribes (app.py:246-247). It never calls `state.get`, which its role allows.
- After a shell start, restart or reconnect, the tray shows those defaults until the next change event. `set_connected(False)` zeroes the capture flags (logic.py:129-136), and nothing restores them.
- The CAPTURING tint reflects capture *permission* (`privacy.capture_changed.microphone`), not an open device. In `ptt_only` that flag is True while the mic is closed. Whether the tray shows "capturing" therefore depends on whether any zone or mode change happened since the shell connected.
- SAFE_MODE is not restored after a shell restart during a kill.

**R16 (M) The shell ignores the user's configuration.**
- `load_config` reads `NOX_CONFIG` or the repo's `config/defaults.yaml` only (shell/runtime.py:104-124), and the supervisor never sets `NOX_CONFIG`.
- `voice.stt.push_to_talk_hotkey` is editable in the dashboard (settings/schema.py:39) but never reaches the hotkey listener, even after a restart.
- The `pet.variant` fallback reload (app.py:322-339) re-reads the same defaults file.
- `pet.default_monitor`, `always_on_top`, `click_through_when_idle`, `fps_target` and `fps_in_game` (core/config/assistant.py:150-154) are consumed nowhere.

**R17 (M) Hotkey robustness.**
- pynput hooks the keyboard without suppressing keys, so there is no OS-level conflict detection: combos also reach the focused app or game.
- Low-level hooks are blind while an elevated window has focus (UIPI), so PTT and the shell's kill hotkey do nothing in admin-launched or anti-cheat games. The supervisor's hotkey is also pynput.
- Stuck keys:
  - `HotkeyTracker.reset()` is never called and throws away the PTT release it computes (hotkeys.py:84-90).
  - A missed key-up (Win+L or a UAC prompt while holding the combo) leaves the key in `_down`.
  - The result is a stuck PTT: the mic stays open, and in `ptt_only` every VAD segment is transcribed with decision PTT until the next press/release.
  - A stale modifier also lets partial combos fire; a stuck Ctrl turns Alt+Shift+K into a kill.
- Internal conflicts raise ValueError inside `ShellApp.__init__` (logic.py:199-206, app.py:141-143), which would put the shell into a crash loop under the supervisor.
- Unverified: Ctrl+letter arrives from pynput as a control character (for example `'\x0b'`), and `_key_name` prefers `char` over `vk` (hotkeys.py:119-129), so the "k" in Ctrl+Alt+Shift+K may never match. The supervisor's own hotkey covers kill. Both processes fire on that combo, so the kill is engaged twice.

**R18 (L-M) Multi-monitor and DPI.** The pet position is restored blindly (pet_window.py:88) with no check against the current screens. After undocking or unplugging a monitor, the pet is off-screen, and the tray has no "reset position" action. `default_monitor` is unused, and mixed-DPI placement relies on Qt defaults.

**R19 (M, honesty and privacy) Whisper downloads itself.**
- `WhisperModel("small", download_root=…, local_files_only=False)` (faster_whisper_engine.py:70-77) pulls from Hugging Face on the first start without a model.
- The download runs from the worker process, outside the core's egress policy, including in offline privacy mode.
- This contradicts README.md:219 ("Nothing is ever downloaded on its own") and voice/models.py:3.

**R20 (L) Kokoro download has no integrity check.** `KOKORO_FILES` carries expected sizes (models.py:30-33) that are never compared, there is no hash, and any existing file with size > 0 is accepted (87).

**R21 (M, hardware) PTT clips the start of speech and misbehaves with Bluetooth.**
- In `ptt_only` the device opens *at* press (pipeline.py:369-371 → audio.py:353-360), so pre-roll is empty. Device open time (roughly 50-300 ms, 1-3 s for a Bluetooth A2DP→HFP profile switch) cuts off the first syllable, often the word "Nox".
- The core→worker request has a 2 s timeout (handlers/core.py:331); a slow Bluetooth open fails silently in the shell.
- Bluetooth headsets flip audio profile on every press, which degrades game audio.

**R22 (L) Per-sentence output stream.** A new stream per utterance (audio.py:266-294) adds a device-open gap or click between sentences that the "90 ms to first audio" figure does not include. A vanished device holds `_say_lock` for up to the 30 s drain timeout (audio.py:190,279-283).

**R23 (L-M) Mixed German/English TTS.**
- One voice per sentence, chosen from the *user's* Whisper-detected language (orchestrator.py:371-380). A short English-detected question ("Nox, what's up") gets a German answer read by `en_US-lessac`.
- English game terms are read by thorsten with German phonemes.
- Setting `voice.tts.voice` drops Piper's EN voice (worker/main.py:494).
- Kokoro uses the `en-us` phonemizer for German (kokoro_engine.py:53-68,186-187). Health reports this as LIMITED, which is honest.

**R24 (L) Sensors.**
- `GameProcessSensor.poll` runs `psutil.process_iter` on the core event loop (sensors/game.py:89) every 5 s, roughly 20-100 ms on Windows, which stalls IPC.
- `sensors.status.read` exposes foreground process names while a zone is active (foreground.py:93,123 + tools.py), for example `KeePass.exe` visible to a cloud LLM.
- GPU is NVIDIA-only, and `system.gpu`/`vram_mb` are written as 0.0 when unknown (resources.py:145-148), which looks the same as an idle GPU and is not flagged in health.

**R25 (L) Tooling honesty.**
- Selftest lists only "Windows WASAPI" devices (worker/main.py:611), so the list is empty on macOS and Linux. It also uses `VoiceConfig()` defaults instead of the user's merged config (705, 739).
- `spikes/sp02_stt.py:54` assigns `mic.enabled = True` to a read-only property, so `--record` crashes.
- heartbeat.py:3 says "the core marks a worker unavailable after three missed heartbeats", but `worker_heartbeat` is a no-op (ipc/handlers/core.py:416-417). Liveness is only the WebSocket ping (20 s interval, 20 s timeout; ipc/server.py:394-395), so detection takes about 40 s.

**R26 (L) Race on `_pending`.** `SoundDeviceInput._pending` is mutated on the PortAudio thread (audio.py:408,414) and reset on the loop thread (363). Python-level resampling and allocation in the callback, under GIL contention, can cause overflows, and `dropped_frames` is never reported.

---

## 4. Gaps, honest-status mismatches, TODOs and stubs

**No TODO, FIXME, XXX or HACK markers** exist in `src/nox/{voice,sensors,shell,worker}` or the in-scope tests and spikes; grep found none.

Stubs and dead surface:
- `_NoEngine` placeholder (worker/main.py:503-526).
- `Transcript.partial` is never set (base.py:32).
- `stt.engine`, `stt.vad` and `tts.streaming` are accepted and never read (assistant.py:40-45,68-73).
- `PipelineConfig.require_wake_word` and `end_silence_ms`, `max_utterance_ms`, `min_utterance_ms` are not wired from config (worker/main.py:427-433).
- `SoundDeviceOutput.forget_devices` (audio.py:208) and `AudioOutput.devices()` have no IPC exposure.
- The pet config keys listed in R16 are unused.
- `Win32Probe` and `SensorsBundle` are compatibility aliases.
- `HotkeyTracker.reset` has no caller.

Claims versus behaviour:

| Claim | Reality |
|---|---|
| "a missing model is an `unavailable` health reason that names the path" (README.md:219) | The worker crashes; core health says "worker not running" (R2). |
| "Nothing is ever downloaded on its own" (README.md:219, models.py:3) | Whisper downloads itself (R19). |
| "A failing microphone capture loop is reported … health check says unavailable" (CHANGELOG.md:331) | `capture_health` is never read outside tests (R6). |
| "Every `tts.speak` ends in exactly one terminal event" (CHANGELOG) | Two events on an unexpected error (R8). |
| "a `tts.stop` that arrives while an utterance is still queued now cancels that utterance" (CHANGELOG) | Defeated by the pipeline `_say_lock` (R3). |
| "Follow-up questions may skip the wake word" (pipeline.py:349, defaults.yaml:111) | Dropped as unaddressed (R4). |
| wake gate "reports `limited`" (README.md:250) | Only in the log and selftest; not in core health (R6). |
| PLATFORMS.md:24/34 on Wayland | The kill phrase and PTT are unreachable, and the microphone is also fail-closed (R12). |
| "the worker picks the flag up when it reconnects" (handlers/core.py:345) | Not implemented (R1). |
| Shell tooltip/tint "capture indicator" | Shows permission, and resets on reconnect (R15). |
| Dashboard-editable PTT hotkey | Ignored by the shell (R16). |
| "core marks unavailable after three missed heartbeats" (heartbeat.py:3) | The handler is a no-op (R25). |
| Idle comment "GetTickCount wrapped (49.7 days)" (win32.py:62) | The actual failure starts at 24.9 days (R14). |

---

## 5. What "reliable voice" needs next, tied to the current extension points

1. **Wake word.** Train a "Nox" (or "Hey Nox") openWakeWord model with the `openwakeword[full]` toolchain: synthetic Piper/Kokoro positives plus negatives from German speech and game audio. Drop it into `<models_dir>/openwakeword/`; `build_detector` (wake_gate.py:171-192) already loads it.
   - Before relying on it, fix R4 by adding `GateDecision.CONVERSATION` to `addressed` at pipeline.py:344, and add a negative test.
   - Record false accepts per hour and false rejects on real audio; tune `wake_word_threshold` and `wake_window_s` from those numbers.
   - Check that the openWakeWord feature models (melspectrogram and embedding ONNX) are present, not only the wake model.
   - Add a separate short acoustic model for the kill phrase, so the ≤ 2.5 s Whisper watchdog and its false negatives (R11) are no longer needed.
2. **Echo control.**
   - Short term, add half-duplex gating inside the `_listen` loop (pipeline.py:226-257): while `_speaking`, raise the VAD margin or require N frames above the TTS reference energy, or ignore segments that fall inside TTS playback plus a hangover.
   - Real fix, on Windows: capture through a WASAPI communications stream (system AEC), or feed the playback PCM from `_DevicePlayer.feed` (audio.py:151-153) into a WebRTC AEC3/speexdsp canceller before `Segmenter.push`. The playback reference already passes through one process, which makes a canceller practical.
   - Until then, recommend headphones in the UI when `listening_mode: continuous`.
3. **Barge-in semantics.** Make `interrupt` cancel the whole turn: track the `request_id` prefix of `utterance_id` (orchestrator.py:375) and drop queued `say` tasks for it (pipeline.py:396-439; worker `_say_tasks`). Alternatively, send one streaming `tts.speak` per turn.
4. **Speaker verification.** Gate barge-in and the kill phrase on a speaker embedding (for example an ECAPA ONNX model) computed per segment in `_transcribe_loop` before STT. The `WakeGate.decide` return value is the natural extension point. This also reduces self-triggering and stream/TV false positives.
5. **VAD and streaming STT.**
   - Replace `EnergyVad` with Silero VAD via ONNX; the `Segmenter.vad` field is injectable.
   - Bound `_pending` (drop oldest or merge) and report the backlog.
   - Add a `no_speech_prob`/confidence gate and a hallucination blacklist in `FasterWhisperStt.transcribe`.
   - For latency, run incremental decoding on the growing segment buffer and emit `Transcript(partial=True)`; the field already exists in base.py:32.
   - Pin `language` to `de` for short watchdog and kill segments.
6. **German TTS quality.**
   - Keep Piper thorsten, and evaluate `de_DE-thorsten-high` or `-emotional`.
   - Add per-segment language tagging in `split_sentences`, so English terms get the English voice. `TtsRequest.language` is per request today, so the split would happen in the orchestrator's `_speak`.
   - Decide the reply language from the reply text, not from the user's utterance.
   - Kokoro stays English-only until a German voice pack exists.
7. **Device robustness.**
   - Add an input-device config key.
   - Add a frame watchdog: no frame for more than 1 s while enabled means reopen, and set `capture_error` if that fails.
   - Handle WASAPI auto-convert or retry output at 48 kHz with resampling; call `forget_devices` on a device-change notification (IMMNotificationClient on Windows).
   - Keep the output stream open across sentences of one turn.
8. **Status plumbing.**
   - Send `capture_health`, `wake_gate_health`, engine `health()`, `dropped_frames`, underruns and backlog depth in the heartbeat or `worker.ready`, and feed them into `voice_check` (health.py:58-70).
   - Push the privacy, mute and kill snapshot in the `worker.register` reply (fixes R1).
   - Add supervised respawn with backoff for the voice worker (fixes R2).
   - Have the shell call `state.get` on connect (fixes R15).

---

## 6. Test coverage map

Test file totals are approximate `def test_` counts; parametrised cases add more. CI syncs `dev, shell, rl` only, not `voice` (ci.yml:35,95), and deselects `hardware`.

| Area | Covered, with fakes | Not covered |
|---|---|---|
| VAD and segmenter | tone/noise/hiss, pre-roll, max length, clicks, forced PTT (test_vad.py, 8) | Real voices, quiet or far mics, game audio, AGC |
| Wake word and kill text | variants, greetings, mid-sentence, kill phrases (test_wake_word.py) | Filler words, misspellings (R11 cases), kill phrase inside a question (false positive) |
| Wake gate | windows, threshold, watchdog, fallback, model discovery (test_wake_gate.py, 16) | Real openWakeWord inference; `addressed` on CONVERSATION (R4) |
| Pipeline | gate, mute, privacy refresh, kill latch, PTT, single-utterance barge-in, STT failure, capture failure, one-event-on-error (test_pipeline*.py, test_capture_gate_hardware.py) | Multi-sentence barge-in (R3), echo, backlog bounds, device stall (R6) |
| Worker | register, heartbeat, handlers, kill/panic/capture events, config fallback, reconnect/re-register, connect retry (test_worker*.py) | Initial privacy/mute sync (R1), engine-load failure (R2), duplicate `tts.finished` (R8), crash and respawn |
| Engines | Kokoro with a fake `kokoro_onnx` (20), clip id validation, model paths | FasterWhisperStt language forcing and hallucination handling, PiperTts synthesis, SoundDeviceInput/Output (none in CI) |
| Sensors | probe selection per platform, X11 and mac parsing, foreground redaction and zones, idle stages, resources, game, history, tool, poll loop, integration zone→privacy→pet | `RealWin32Probe` entirely (tick wrap, R14), elevated or UWP windows, psutil timing |
| Shell | offscreen Qt: connect, ping, reconnect, token change, tray tint, permissions, kill via core or supervisor, quit, hotkey→request mapping, pure tracker, dialogs, runtime files, pet profile hardening, click-through bits | Real pynput, stuck keys, state resync (R15), user config (R16), multi-monitor, DPI |

**What only real hardware or a real desktop can test:**
- Microphone open latency in `ptt_only` (R21) and Bluetooth HFP switching.
- WASAPI/MME sample-rate acceptance for named devices (R7).
- Hot-plug and default-device changes mid-stream (R6).
- Real barge-in stop latency; the < 200 ms hardware test is opt-in.
- Echo and self-barge-in with speakers (R5).
- Whisper `small` CPU latency and real-time factor on the target CPU; no committed number exists.
- Hallucination rate on PTT silence (R10).
- Kill-phrase recognition rate across speakers and noise (R11).
- openWakeWord false accepts and false rejects with a trained model.
- Piper and Kokoro time to first audio including device open.
- pynput behaviour with elevated or anti-cheat games, Win+L and UAC (R17), German layout and AltGr.
- GetTickCount beyond 24.9 days of uptime (R14; can be unit-tested by mocking the ctypes return).
- Pet transparency, click-through and FPS on multi-monitor and mixed-DPI setups (only single-monitor sp09 exists).
- Wayland/X11/macOS permission flows (docs mark these ◐).
