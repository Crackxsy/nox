"""The voice satellite: Nox listens and speaks on this device; only text crosses the network.

With the group `voice` enabled on this device (`nox node allow voice`, off by default) - and
switched on for it on the core as well, where it starts switched off (`nox.node.hub.policy`,
`CORE_OPT_IN`) - the node runs the core's own voice pipeline (`nox.voice.pipeline`) here:

* **Listening** - push-to-talk (the microphone is open only while it is held) or the wake word
  (the microphone stays open behind the wake-word gate). `mode: auto`, the default, is
  push-to-talk on a device with a screen (the tray's key, a terminal) and the wake word on one
  without (a headless Pi has no key to hold; `listening_mode`). Wake word, voice activity and
  speech recognition (faster-whisper; `tiny` on a Pi, `base` elsewhere unless set) run on this
  device. Only the recognised **text** goes to the core (`nox/voice/heard`); audio never leaves
  the device and is never written anywhere - it lives in memory for one utterance.
* **Who may open the microphone** - all of: the group is enabled here and not narrowed by the
  core, the node is not stopped, it is connected to a core that understands satellites, the
  core's privacy allows the microphone right now (`nox/voice/gate`: off until the core says
  so - panic, safe mode, the capture switch, a privacy zone and the kill switch close it), and
  the local mute switch is off (`nox node voice mute`, the tray). Only this device unmutes.
* **Speaking** - the core's answer comes back as text, sentence by sentence (`nox/voice/say`),
  accepted only for a question this device asked in the last minutes; this device synthesises it
  (Supertonic 3, voice F3, by default; Piper when Supertonic is missing - the core's AI-voice
  disclosure rule for streams applies to that voice like to the core's own). `voice.say` lets the
  model say something here on purpose (low risk, receipt, budget, audit). The loudness of what is
  played goes to the core as a number (`nox/voice/level`), for a pet on this device.
* **Seen and heard** - while the microphone is open the tray shows it ("Nox hört zu",
  `voice.json` in the node's folder) and, with `earcon` on, a short tone says so when push-to-talk
  starts and when a question was taken.
* **"Nox, Notaus"** never reaches the core as text: it stops this node at once (as
  `nox node stop`), which stops Nox everywhere; only this device resumes.

**Models** come only from `nox node voice setup` (asks first, downloads into the node's folder):
the node loads faster-whisper with `local_files_only` and never fetches a model by itself.
`voice.say` is offered only where the voice libraries are installed (`voice_support`).

No transcript is stored on this device; the node's audit records that something was heard and
how long it was, never what. On the core the turn follows its privacy mode like a turn spoken
into the core's own microphone, except that facts learned from it wait for confirmation
(`turn_origin("node")`).

The imports inside functions in this package are deliberate. They are not cycle workarounds: the
speech stack (faster-whisper, sounddevice, numpy, the voice pipeline and its models) belongs to the
optional extras `voice` and `voice-supertonic`, so a device without them must still import these
modules and report what is missing (`settings.voice_support`) instead of failing to start.

The package is cut along the parts named above:

* `settings` - `VoiceSettings`, the marker files, the listening mode and what this device can do.
* `engines` - the core's engines, loaded on this device.
* `satellite` - `VoiceSatellite` (listening, speaking, mute, push-to-talk) and `voice_tools`.
* `setup` - `nox node voice setup`: the models, downloaded only after a yes.
"""
